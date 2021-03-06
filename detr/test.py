# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import os
import argparse
import random
from pathlib import Path
import sys
import glob

sys.path.append("./DETR")
import numpy as np
import cv2
import torch
from torchvision import transforms
from torch.utils.data import DataLoader, DistributedSampler

import DETR.datasets
import DETR.util.misc as utils
from DETR.datasets import build_dataset, get_coco_api_from_dataset
from DETR.engine import evaluate, train_one_epoch
from DETR.models import build_model
from DETR.models.backbone import build_backbone
from DETR.models.matcher import build_matcher
from DETR.models.transformer import build_transformer
from DETR.models.detr import DETR as DETRModel


def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector',
                                     add_help=False)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=2, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=300, type=int)
    parser.add_argument('--lr_drop', default=200, type=int)
    parser.add_argument('--clip_max_norm',
                        default=0.1,
                        type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument(
        '--frozen_weights',
        type=str,
        default=None,
        help=
        "Path to the pretrained model. If set, only the mask head will be trained"
    )
    # * Backbone
    parser.add_argument('--backbone',
                        default='resnet50',
                        type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument(
        '--dilation',
        action='store_true',
        help=
        "If true, we replace stride with dilation in the last convolutional block (DC5)"
    )
    parser.add_argument(
        '--position_embedding',
        default='sine',
        type=str,
        choices=('sine', 'learned'),
        help="Type of positional embedding to use on top of the image features"
    )

    # * Transformer
    parser.add_argument('--enc_layers',
                        default=6,
                        type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--dec_layers',
                        default=6,
                        type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument(
        '--dim_feedforward',
        default=2048,
        type=int,
        help=
        "Intermediate size of the feedforward layers in the transformer blocks"
    )
    parser.add_argument(
        '--hidden_dim',
        default=256,
        type=int,
        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout',
                        default=0.1,
                        type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument(
        '--nheads',
        default=8,
        type=int,
        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries',
                        default=100,
                        type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')

    # * Segmentation
    parser.add_argument('--masks',
                        action='store_true',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument(
        '--no_aux_loss',
        dest='aux_loss',
        action='store_false',
        help="Disables auxiliary decoding losses (loss at each layer)")
    # * Matcher
    parser.add_argument('--set_cost_class',
                        default=1,
                        type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox',
                        default=5,
                        type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou',
                        default=2,
                        type=float,
                        help="giou box coefficient in the matching cost")
    # * Loss coefficients
    parser.add_argument('--mask_loss_coef', default=1, type=float)
    parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument(
        '--eos_coef',
        default=0.1,
        type=float,
        help="Relative classification weight of the no-object class")

    # dataset parameters
    parser.add_argument('--dataset_file', default='coco')
    parser.add_argument('--coco_path', type=str)
    parser.add_argument('--coco_panoptic_path', type=str)
    parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir',
                        default='',
                        help='path where to save, empty for no saving')
    parser.add_argument('--device',
                        default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch',
                        default=0,
                        type=int,
                        metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=2, type=int)

    # distributed training parameters
    parser.add_argument('--world_size',
                        default=1,
                        type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url',
                        default='env://',
                        help='url used to set up distributed training')
    return parser


def main(args):
    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    backbone = build_backbone(args)

    transformer = build_transformer(args)
    num_classes = 20 if args.dataset_file != 'coco' else 91
    print(f'args : {args}')
    model = DETRModel(
        backbone,
        transformer,
        num_classes=num_classes,
        num_queries=args.num_queries,
        aux_loss=args.aux_loss,
    )

    model_without_ddp = model
    if args.resume:
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(args.resume,
                                                            map_location='cpu',
                                                            check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'])
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1

    img_paths = glob.glob(
        os.path.join(args.coco_path, 'val2017', '000000229*jpg'))
    for img_path in img_paths:
        img = cv2.imread(img_path)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        # img = img.transpose(2, 0, 1)
        # img = np.ascontiguousarray(img, dtype=np.float32)
        # img /= 255.0
        # img = img[np.newaxis, :, :, :]

        img_th = transforms.ToTensor()(img)
        img_th = img_th.unsqueeze(0)
        print(f'img : {img_th.shape}')
        outputs = model(utils.NestedTensor(tensors=img_th, mask=None))
        print(type(outputs))
        # model_without_ddp
    print(model)


if __name__ == "__main__":
    parser = argparse.ArgumentParser('DETR training and evaluation script',
                                     parents=[get_args_parser()])
    args = parser.parse_args()
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
