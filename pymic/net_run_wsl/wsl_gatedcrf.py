# -*- coding: utf-8 -*-
from __future__ import print_function, division
import logging
import numpy as np
import torch
from pymic.loss.seg.util import get_soft_label
from pymic.loss.seg.util import reshape_prediction_and_ground_truth
from pymic.loss.seg.util import get_classwise_dice
from pymic.loss.seg.gatedcrf import ModelLossSemsegGatedCRF
from pymic.net_run_wsl.wsl_abstract import WSLSegAgent
from pymic.util.ramps import sigmoid_rampup

class WSLGatedCRF(WSLSegAgent):
    """
    Training and testing agent for semi-supervised segmentation
    """
    def __init__(self, config, stage = 'train'):
        super(WSLGatedCRF, self).__init__(config, stage)
        # parameters for gated CRF 
        wsl_cfg = self.config['weakly_supervised_learning']
        w0 = wsl_cfg.get('GatedCRFLoss_W0'.lower(), 1.0)
        xy0= wsl_cfg.get('GatedCRFLoss_XY0'.lower(), 5)
        rgb= wsl_cfg.get('GatedCRFLoss_rgb'.lower(), 0.1)
        w1 = wsl_cfg.get('GatedCRFLoss_W1'.lower(), 1.0)
        xy1= wsl_cfg.get('GatedCRFLoss_XY1'.lower(), 3)
        kernel0 = {'weight': w0, 'xy': xy0, 'rgb': rgb}
        kernel1 = {'weight': w1, 'xy': xy1}
        self.kernels = [kernel0, kernel1]
        self.radius  = wsl_cfg.get('GatedCRFLoss_Radius'.lower(), 5.0)

    def training(self):
        class_num   = self.config['network']['class_num']
        iter_valid  = self.config['training']['iter_valid']
        wsl_cfg     = self.config['weakly_supervised_learning']
        train_loss  = 0
        train_loss_sup = 0
        train_loss_reg = 0
        train_dice_list = []

        gatecrf_loss = ModelLossSemsegGatedCRF()
        self.net.train()
        for it in range(iter_valid):
            try:
                data = next(self.trainIter)
            except StopIteration:
                self.trainIter = iter(self.train_loader)
                data = next(self.trainIter)
            
            # get the inputs
            inputs = self.convert_tensor_type(data['image'])
            y      = self.convert_tensor_type(data['label_prob'])  
                         
            inputs, y = inputs.to(self.device), y.to(self.device)
            
            # zero the parameter gradients
            self.optimizer.zero_grad()
                
            # forward + backward + optimize
            outputs = self.net(inputs)
            loss_sup = self.get_loss_value(data, outputs, y)

            # for gated CRF loss, the input should be like NCHW
            outputs_soft = torch.softmax(outputs, dim=1)
            input_shape  = list(inputs.shape)
            if(len(input_shape) == 5):
                [N, C, D, H, W] = input_shape
                new_shape  = [N*D, C, H, W]
                inputs = torch.transpose(inputs, 1, 2)
                inputs = torch.reshape(inputs, new_shape)
                [N, C, D, H, W] = list(outputs_soft.shape)
                new_shape    = [N*D, C, H, W]
                outputs_soft = torch.transpose(outputs_soft, 1, 2)
                outputs_soft = torch.reshape(outputs_soft, new_shape)
            batch_dict = {'rgb': inputs}
            loss_reg = gatecrf_loss(outputs_soft, self.kernels, self.radius,
                batch_dict,input_shape[-2], input_shape[-1])["loss"]
            
            iter_max = self.config['training']['iter_max']
            ramp_up_length = wsl_cfg.get('ramp_up_length', iter_max)
            regular_w = 0.0
            if(self.glob_it > wsl_cfg.get('iter_sup', 0)):
                regular_w = wsl_cfg.get('regularize_w', 0.1)
                if(ramp_up_length is not None and self.glob_it < ramp_up_length):
                    regular_w = regular_w * sigmoid_rampup(self.glob_it, ramp_up_length)
            loss = loss_sup + regular_w*loss_reg

            loss.backward()
            self.optimizer.step()
            self.scheduler.step()

            train_loss = train_loss + loss.item()
            train_loss_sup = train_loss_sup + loss_sup.item()
            train_loss_reg = train_loss_reg + loss_reg.item() 
            # get dice evaluation for each class in annotated images
            if(isinstance(outputs, tuple) or isinstance(outputs, list)):
                outputs = outputs[0] 
            p_argmax = torch.argmax(outputs, dim = 1, keepdim = True)
            p_soft   = get_soft_label(p_argmax, class_num, self.tensor_type)
            p_soft, y = reshape_prediction_and_ground_truth(p_soft, y) 
            dice_list   = get_classwise_dice(p_soft, y)
            train_dice_list.append(dice_list.cpu().numpy())
        train_avg_loss = train_loss / iter_valid
        train_avg_loss_sup = train_loss_sup / iter_valid
        train_avg_loss_reg = train_loss_reg / iter_valid
        train_cls_dice = np.asarray(train_dice_list).mean(axis = 0)
        train_avg_dice = train_cls_dice.mean()

        train_scalers = {'loss': train_avg_loss, 'loss_sup':train_avg_loss_sup,
            'loss_reg':train_avg_loss_reg, 'regular_w':regular_w,
            'avg_dice':train_avg_dice,     'class_dice': train_cls_dice}
        return train_scalers
        