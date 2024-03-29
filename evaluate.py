import argparse
import scipy
from scipy import ndimage
import cv2
import numpy as np
import sys
import json
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import torchvision.models as models
import torch.nn.functional as F
from torch.utils import data
import networks
from dataset.datasets import KvasirSegDataSet
from collections import OrderedDict
import os
import scipy.ndimage as nd
from math import ceil
from PIL import Image as PILImage
from utils.pyt_utils import load_model
from utils.image_utils import get_val_merged_image
from engine import Engine
from metric import get_confusion_matrix,calculate_metrics

IMG_MEAN = np.array((104.00698793,116.66876762,122.67891434), dtype=np.float32)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def pad_image(img, target_size):
    """Pad an image up to the target size."""
    rows_missing = target_size[0] - img.shape[2]
    cols_missing = target_size[1] - img.shape[3]
    padded_img = np.pad(img, ((0, 0), (0, 0), (0, rows_missing), (0, cols_missing)), 'constant')
    return padded_img

def predict_sliding(net, image, tile_size, classes, recurrence):

    interp = nn.Upsample(size=tile_size, mode='bilinear', align_corners=True) #
    image_size = image.shape #(1, 3, 1024, 2048)
    overlap = 1/3

    stride = ceil(tile_size[0] * (1 - overlap)) #683 354

    tile_rows = int(ceil((image_size[2] - tile_size[0]) / stride) + 1)  # strided convolution formula 1

    tile_cols = int(ceil((image_size[3] - tile_size[1]) / stride) + 1) #1

    full_probs = np.zeros((image_size[0], image_size[2], image_size[3], classes)) #1, 1024, 2048, 2)

    count_predictions = np.zeros((1, image_size[2], image_size[3], classes)) #(1, 1024, 2048, 2)

    tile_counter = 0

    for row in range(tile_rows):
        for col in range(tile_cols):
            x1 = int(col * stride) #0
            y1 = int(row * stride) #0
            x2 = min(x1 + tile_size[1], image_size[3]) #2048
            y2 = min(y1 + tile_size[0], image_size[2]) #1024
            x1 = max(int(x2 - tile_size[1]), 0)  # for portrait images the x1 underflows sometimes  0
            y1 = max(int(y2 - tile_size[0]), 0)  # for very few rows y1 underflows  0

            img = image[:, :, y1:y2, x1:x2]  #(1, 3, 1024, 2048) 
            padded_img = pad_image(img, tile_size) #padded_img (1, 3, 1024, 2048)

            # plt.imshow(padded_img)
            # plt.show()
            tile_counter += 1
            # print("Predicting tile %i" % tile_counter)
            padded_prediction = net(torch.from_numpy(padded_img)) #eeee list 2

            if isinstance(padded_prediction, list):
                padded_prediction = padded_prediction[0] #torch.Size([1, 2, 129, 257])

            padded_prediction = interp(padded_prediction).cpu().numpy().transpose(0,2,3,1) #(1, 1024, 2048, 2)

            prediction = padded_prediction[0, 0:img.shape[2], 0:img.shape[3], :] #(1024, 2048, 2)
            count_predictions[0, y1:y2, x1:x2] += 1 #(1, 1024, 2048, 2)

            full_probs[:, y1:y2, x1:x2] += prediction  # accumulate the predictions also in the overlapping regions (1, 1024, 2048, 2)


    # average the predictions in the overlapping regions
    full_probs /= count_predictions #(1, 1024, 2048, 2)

    # visualize normalization Weights
    # plt.imshow(np.mean(count_predictions, axis=2))
    # plt.show()
    return full_probs

def predict_whole(net, image, tile_size, recurrence):
    N_, C_, H_, W_ = image.shape
    image = torch.from_numpy(image)
    interp = nn.Upsample(size=(H_, W_), mode='bilinear', align_corners=True)
    prediction = net(image.cuda())
    if isinstance(prediction, list):
        prediction = prediction[0]
    prediction = interp(prediction).cpu().numpy().transpose(0,2,3,1)
    return prediction

def predict_multiscale(net, image, tile_size, scales, classes, flip_evaluation, recurrence):
    """
    Predict an image by looking at it with different scales.
        We choose the "predict_whole_img" for the image with less than the original input size,
        for the input of larger size, we would choose the cropping method to ensure that GPU memory is enough.
    """
    image = image.data #[1, 3, 1024, 2048]

    N_, C_, H_, W_ = image.shape #1 3 1024 2048
    full_probs = np.zeros((N_, H_, W_, classes))  #(1, 1024, 2048, 2)


    for scale in scales:
        scale = float(scale) #1.0

        scale_image = ndimage.zoom(image, (1.0, 1.0, scale, scale), order=1, prefilter=False) #(1, 530, 621, 3)

        scaled_probs = predict_sliding(net, scale_image, tile_size, classes, recurrence) #(1, 530, 621, 2)

        full_probs += scaled_probs #(1, 530, 621, 2)

    full_probs /= len(scales) #(1, 530, 621, 2)

    return full_probs


def validation_method(epoch, args, model, test_loader, criterion, summary_writer):

    """Create the model and start the evaluation process."""

    model.eval()

    h, w = map(int, args.input_size.split(','))

    data_list = []
    confusion_matrix = np.zeros((args.num_classes,args.num_classes))

    bar_format = '{desc}[{elapsed}<{remaining},{rate_fmt}]'
    pbar = tqdm(range(len(test_loader)), file=sys.stdout,
                bar_format=bar_format)
    dataloader = iter(test_loader)
    val_loss_sum = 0
    for idx in pbar:

        original_image, image, label, size, name = dataloader.next() #[1, 3, 1024, 2048] #[1, 1024, 2048]

        resized_loss, resized_val_metric, ccnet_upsampled = validate_resized(epoch, args, model, test_loader, criterion, summary_writer)

        size = (size[0][0],size[0][1])

        with torch.no_grad():
            output = predict_multiscale(net = model, image = image, tile_size = (h, w), scales = [1.0], classes = args.num_classes, flip_evaluation = False, recurrence = 0) #(1, 530, 621, 2)
        
        output = output.transpose(0,3,1,2)

        loss = criterion(preds=output, target=label)
        reduce_loss = loss.data
        val_loss_sum += reduce_loss.item()

        #metric
        pred = np.asarray(np.argmax(output, axis=1), dtype=np.uint8) #(1, 1024, 2048)
        gt = np.asarray(label.numpy()[:,:size[0],:size[1]], dtype=np.int) #(1, 530, 621)
    
        ignore_index = gt != 255

        seg_gt = gt[ignore_index]

        seg_pred = pred[ignore_index] #seg_pred (1, 530, 621) seg_gt (1, 530, 621)

        confusion_matrix += get_confusion_matrix(seg_gt, seg_pred, args.num_classes, ignore_label = args.ignore_label)


        print_str = ' Iter{}/{}'.format(idx + 1, len(test_loader))

        merged_image = get_val_merged_image(original_image, gt, pred, ccnet_upsampled)

        summary_writer.add_image(tag="eval_"+name[0], img_tensor = merged_image, global_step=epoch)

    #for metric

    tn, fp, fn, tp, meanIU, dice, prec, recall = calculate_metrics(confusion_matrix)
    val_loss = round(val_loss_sum / len(test_loader), 6)

    val_metric = {  "tn":tn,
                    "fp": fp,
                    "fn": fn,
                    "tp": tp,
                    "meanIU": meanIU,
                    "dice" : dice,
                    "precision": prec,
                    "recall": recall,

                    "resized_tn":resized_val_metric["tn"],
                    "resized_fp":resized_val_metric["fp"],
                    "resized_fn":resized_val_metric["fn"],
                    "resized_tp":resized_val_metric["tp"],
                    "resized_iou": resized_val_metric["meanIU"],
                    "resized_dice": resized_val_metric["dice"],
                    "resized_precision": resized_val_metric["prec"],
                    "resized_recall": resized_val_metric["recall"]
                }


    return val_loss, val_metric
    



def validate_resized(epoch, args, model, test_loader, criterion, summary_writer):
    upsample = nn.Upsample(size=args.input_size, mode='bilinear', align_corners=True) #

    image = cv2.resize(image, args.input_size, interpolation = cv2.INTER_LINEAR)
    output = model(image)
    ccnet_out = output[0]
    dsn_out = output[1]

    resize_loss = criterion(preds=ccnet_out, target=label)
    reduce_loss = resize_loss.data
    resized_loss = reduce_loss.item()

    #metric
    ccnet_upsampled = upsample(ccnet_out)
    print("ccnet_upsampled",ccnet_upsampled.shape)

    resize_pred = np.asarray(np.argmax(ccnet_upsampled, axis=1), dtype=np.uint8) #(1, 1024, 2048) ori

    confusion_matrix += get_confusion_matrix(resize_pred, label, args.num_classes, ignore_label = args.ignore_label)

    tn, fp, fn, tp, meanIU, dice, prec, recall = calculate_metrics(confusion_matrix)

    resized_val_metric = {  "tn":tn,
                    "fp": fp,
                    "fn": fn,
                    "tp": tp,
                    "meanIU": meanIU,
                    "dice" : dice,
                    "precision": prec,
                    "recall": recall}


    return resized_loss, resized_val_metric, ccnet_upsampled
