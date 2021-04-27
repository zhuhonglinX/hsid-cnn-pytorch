import os
import argparse
from tqdm import tqdm
import yaml
import numpy as np
import torch
import torch.nn as nn
from torch import optim
from torch.utils.data import DataLoader
import torchvision.transforms as transforms
from torch.utils.tensorboard import SummaryWriter

from network import HSID
from dataset_icvl import TrainData, TestData,
from utils import init_exps, calc_psnr, weights_init_kaiming, save_train, RandGaNoise, ToTensor

CUDA_ID = 2
DEVICE = torch.device(f'cuda:{CUDA_ID}')

TRAIN_CFG = {
    "randga55": {
        "epoch": 100,
        "batch_size": 16,
        "learning_rate": 0.001,
        "train_dir": "../dncnn-icvl/data/train/orig",
        "test_orig_dir": "../dncnn-icvl-2/data/test/orig",
        "test_noise_dir": "../dncnn-icvl-2/data/test/randga55",
        "log_dir": "saved_models/icvl/randga55",
    },
    "randga95": {
        "epoch": 100,
        "batch_size": 16,
        "learning_rate": 0.001,
        "train_dir": "../dncnn-icvl/data/train/orig",
        "test_orig_dir": "../dncnn-icvl-2/data/test/orig",
        "test_noise_dir": "../dncnn-icvl-2/data/test/randga95",
        "log_dir": "saved_models/icvl/randga95",
    },
}

def get_train_val_loaders(args):
    print('Loading dataset...')
    tf = transforms.Compose([
        # FixedGaNoise(70),
        RandGaNoise(55),
        # ImpulseNoise(),
        ToTensor(),
    ])
    train_dataset = TrainData(args['train_dir'], 512, tf)
    train_loader = DataLoader(train_dataset, batch_size=args['batch_size'], shuffle=True)
    val_dataset = TestData(args['test_orig_dir'], args['test_noise_dir'], -1)
    val_loader = DataLoader(val_dataset, batch_size=1, shuffle=False)  # 不打乱按顺序
    return train_loader, val_loader


class Trainer:
    def __init__(self):
        train_cfg = TRAIN_CFG['randga55']
        # init experiments
        base_name = f"pretrained"
        log_dir = init_exps(os.path.join(train_cfg['log_dir'], base_name))
        train_cfg['log_dir'] = log_dir
        writer = SummaryWriter(log_dir)
        # record parameter
        with open(os.path.join(log_dir, 'params.yaml'), 'w') as f:
            yaml.dump(train_cfg, f, default_flow_style=False, allow_unicode=True)
        logger = open(os.path.join(log_dir,'logger.txt'),'w+')
        # build model
        model = HSID(24)
        model.apply(weights_init_kaiming)
        model = model.to(DEVICE)
        # train
        self.train(model, train_cfg, logger, writer)
    
    def train(self, model, args, logger, writer):
        # load dataset
        train_loader, val_loader = get_train_val_loaders(args)

        # set optimizer and loss function
        optimizer = optim.Adam(model.parameters(), lr=args['learning_rate'])
        criterion = nn.MSELoss()
        scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=[30, 100], gamma=0.1, last_epoch=-1)

        model_save_dir = os.path.join(args['log_dir'], 'models')
        if not os.path.exists(model_save_dir):
            os.mkdir(model_save_dir)
        
        print('Start training...', file=logger, flush=True)
        train_bar = tqdm(total=len(train_loader), bar_format="{l_bar}{bar:30}{r_bar}")
        val_bar = tqdm(total=len(val_loader), bar_format="{l_bar}{bar:30}{r_bar}")
        best_psnr = 0.0
        for epoch in range(args['epoch']):
            print('Epoch number {}'.format(epoch), file=logger, flush=True)
            train_bar.set_description(f"[{epoch}/{args['epoch']-1}]")
            val_bar.set_description(f"[{epoch}/{args['epoch']-1}]")
            model.train()
            train_loss = 0.0
            train_psnr = 0.0
            for i, sample in enumerate(train_loader):
                input_im, input_vol, target_im = sample['input_im'].to(DEVICE), sample['input_vol'].to(DEVICE), sample['target_im'].to(DEVICE)

                # forward & backward
                optimizer.zero_grad()
                noise_res = model(input_im, input_vol)
                denoise_img = input_im - noise_res
                loss = criterion(denoise_img, target_im)
                loss.backward()
                optimizer.step()

                # record training info
                train_loss += loss.item()
                denoise_img = denoise_img.detach().cpu().numpy()
                target_im = target_im.detach().cpu().numpy()
                train_psnr += calc_psnr(target_im[0], denoise_img[0])
                train_bar.update(1)
            train_bar.reset()
            scheduler.step()

            train_psnr /= len(train_loader)
            train_loss /= len(train_loader)
            val_loss, val_psnr = self.valid(model, val_loader, val_bar)
            print('[{}/{}] | train_loss: {:.5f} | train_psnr: {:.3f} | val loss: {:.5f} | val_psnr: {:.3f}'
                    .format(epoch, args['epoch']-1, train_loss, train_psnr, val_loss, val_psnr), file=logger, flush=True)

            writer.add_scalar('Loss/train', train_loss, epoch)
            writer.add_scalar('Loss/val', val_loss, epoch)
            writer.add_scalar('PSNR/train', train_psnr, epoch)
            writer.add_scalar('PSNR/val', val_psnr, epoch)
            # save every epoch
            if best_psnr < val_psnr:
                best_psnr = val_psnr
                save_train(model_save_dir, model, optimizer, epoch=epoch)
    
    def valid(self, model, data_loader, val_bar):
        criterion = nn.MSELoss()
        model.eval()
        val_loss = 0.0
        val_psnr = 0.0
        with torch.no_grad():
            for i, sample in enumerate(data_loader):
                input_im, input_vol, target_im = sample['input_im'].to(DEVICE), sample['input_vol'].to(DEVICE), sample['target_im'].to(DEVICE)
                noise_res = model(input_im, input_vol)
                denoise_img = input_im - noise_res
                loss = criterion(denoise_img, target_im)

                val_loss += loss.item()
                denoise_img = denoise_img.detach().cpu().numpy()
                target_im = target_im.detach().cpu().numpy()
                val_psnr += calc_psnr(target_im, denoise_img)
                val_bar.update(1)
            val_bar.reset()
        return val_loss / len(data_loader), val_psnr / len(data_loader)


if __name__ == '__main__':
    Trainer()



