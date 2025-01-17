"""
Copyright (C) 2020 NVIDIA Corporation.  All rights reserved.
Licensed under the NVIDIA Source Code License. See LICENSE at https://github.com/nv-tlabs/lift-splat-shoot.
Authors: Jonah Philion and Sanja Fidler
"""

import torch
from torch import nn
from torchvision.models.resnet import resnet18
import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
import math
from torchvision.utils import save_image
from mmdet3d.models.fusion_layers import apply_3d_transformation
import torch.nn.functional as F
import pdb

from torch.utils.checkpoint import checkpoint


class Up(nn.Module):
    def __init__(self, in_channels, out_channels, scale_factor=2):
        super().__init__()

        self.up = nn.Upsample(scale_factor=scale_factor, mode='bilinear',
                              align_corners=True)

        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x1, x2):
        x1 = F.interpolate(x1, x2.shape[2:],  mode='bilinear', align_corners=True)
        x1 = torch.cat([x2, x1], dim=1)
        return self.conv(x1)

class BevEncode(nn.Module):
    def __init__(self, inC, outC):
        super(BevEncode, self).__init__()

        trunk = resnet18(pretrained=False, zero_init_residual=True)
        self.conv1 = nn.Conv2d(inC, 64, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = trunk.bn1
        self.relu = trunk.relu

        self.layer1 = trunk.layer1
        self.layer2 = trunk.layer2
        self.layer3 = trunk.layer3

        self.up1 = Up(64+256, 256, scale_factor=4)
        self.up2 = nn.Sequential(
            nn.Upsample(scale_factor=2, mode='bilinear',
                              align_corners=True),
            nn.Conv2d(256, 128, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
            nn.Conv2d(128, outC, kernel_size=1, padding=0),
        )

    def forward(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)

        x1 = self.layer1(x)
        x = self.layer2(x1)
        x = self.layer3(x)

        x = self.up1(x, x1)
        x = self.up2(x)

        return x

def gen_dx_bx(xbound, ybound, zbound):
    dx = torch.Tensor([row[2] for row in [xbound, ybound, zbound]])
    bx = torch.Tensor([row[0] + row[2] / 2.0 for row in [xbound, ybound, zbound]])
    nx = torch.LongTensor([(row[1] - row[0]) / row[2] for row in [xbound, ybound, zbound]])

    return dx, bx, nx


def cumsum_trick(x, geom_feats, ranks):
    x = x.cumsum(0)
    kept = torch.ones(x.shape[0], device=x.device, dtype=torch.bool)
    kept[:-1] = (ranks[1:] != ranks[:-1])

    x, geom_feats = x[kept], geom_feats[kept]
    x = torch.cat((x[:1], x[1:] - x[:-1]))

    return x, geom_feats


class QuickCumsum(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, geom_feats, ranks):
        x = x.cumsum(0)
        kept = torch.ones(x.shape[0], device=x.device, dtype=torch.bool)
        kept[:-1] = (ranks[1:] != ranks[:-1])

        x, geom_feats = x[kept], geom_feats[kept]
        x = torch.cat((x[:1], x[1:] - x[:-1]))

        # save kept for backward
        ctx.save_for_backward(kept)

        # no gradient for geom_feats
        ctx.mark_non_differentiable(geom_feats)

        return x, geom_feats

    @staticmethod
    def backward(ctx, gradx, gradgeom):
        kept, = ctx.saved_tensors
        back = torch.cumsum(kept, 0)
        back[kept] -= 1

        val = gradx[back]

        return val, None, None


class CamEncode(nn.Module):
    def __init__(self, D, C, inputC):
        super(CamEncode, self).__init__()
        self.D = D
        self.C = C
        self.depthnet = nn.Conv2d(inputC, self.D + self.C, kernel_size=1, padding=0)

    def get_depth_dist(self, x, eps=1e-20):
        return x.softmax(dim=1)

    def get_depth_feat(self, x):
        # Depth
        x = self.depthnet(x)

        depth = self.get_depth_dist(x[:, :self.D])
        new_x = depth.unsqueeze(1) * x[:, self.D:(self.D + self.C)].unsqueeze(2)
        return depth, new_x

    def forward(self, x):
        depth, x = self.get_depth_feat(x)

        return x, depth


class SELayer(nn.Module):

    def __init__(self, channels, act_layer=nn.ReLU, gate_layer=nn.Sigmoid):
        super().__init__()
        self.conv_reduce = nn.Conv2d(channels, channels, 1, bias=True)
        self.act1 = act_layer()
        self.conv_expand = nn.Conv2d(channels, channels, 1, bias=True)
        self.gate = gate_layer()

    def forward(self, x, x_se):
        x_se = self.conv_reduce(x_se)
        x_se = self.act1(x_se)
        x_se = self.conv_expand(x_se)
        return x * self.gate(x_se)


class LiftSplatShoot(nn.Module):
    def __init__(self, lss=False, final_dim=(900, 1600), camera_depth_range=[4.0, 45.0, 1.0], pc_range=[-50, -50, -5, 50, 50, 3], downsample=4, grid=3, inputC=256, camC=64):
        """
        Args:
            lss (bool): using default downsampled r18 BEV encoder in LSS.
            final_dim: actual RGB image size for actual BEV coordinates, default (900, 1600)
            downsample (int): the downsampling rate of the input camera feature spatial dimension (default (224, 400)) to final_dim (900, 1600), default 4.
            camera_depth_range, img_depth_loss_weight, img_depth_loss_method: for depth supervision wich is not mentioned in paper.
            pc_range: point cloud range.
            inputC: input camera feature channel dimension (default 256).
            grid: stride for splat, see https://github.com/nv-tlabs/lift-splat-shoot.

        """
        super(LiftSplatShoot, self).__init__()
        self.pc_range = pc_range
        self.grid_conf = {
            'xbound': [pc_range[0], pc_range[3], grid],
            'ybound': [pc_range[1], pc_range[4], grid],
            'zbound': [pc_range[2], pc_range[5], grid],
            'dbound': camera_depth_range,
        }
        self.final_dim = final_dim
        self.grid = grid

        dx, bx, nx = gen_dx_bx(self.grid_conf['xbound'],
                               self.grid_conf['ybound'],
                               self.grid_conf['zbound'], )
        self.dx = nn.Parameter(dx, requires_grad=False)
        self.bx = nn.Parameter(bx, requires_grad=False)
        self.nx = nn.Parameter(nx, requires_grad=False)

        self.downsample = downsample
        self.fH, self.fW = self.final_dim[0] // self.downsample, self.final_dim[1] // self.downsample
        self.camC = camC
        self.inputC = inputC
        self.frustum = self.create_frustum()
        self.D, _, _, _ = self.frustum.shape
        #self.camencode = CamEncode(self.D, self.camC, self.inputC)

        # toggle using QuickCumsum vs. autograd
        self.use_quickcumsum = True
        z = self.grid_conf['zbound']
        cz = int(self.camC * ((z[1] - z[0]) // (z[2] - 0.0001)))
        self.lss = lss
        self.bevencode = nn.Sequential(
            nn.Conv2d(cz, cz, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(cz),
            nn.ReLU(inplace=True),
            nn.Conv2d(cz, 512, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, 512, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Conv2d(512, inputC, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(inputC),
            nn.ReLU(inplace=True)
        )
        if self.lss:
            self.bevencode = nn.Sequential(
                nn.Conv2d(cz, camC, kernel_size=1, padding=0, bias=False),
                nn.BatchNorm2d(camC),
                BevEncode(inC=camC, outC=inputC)
            )
        self.dtransform = nn.Sequential(  # 提取深度特征
            nn.Conv2d(5, 8, 5, padding=2),
            nn.BatchNorm2d(8),
            nn.ReLU(True),
            nn.Conv2d(8, 32, 5, stride=2, padding=2),
            nn.BatchNorm2d(32),
            nn.ReLU(True),
            nn.Conv2d(32, 64, 5, stride=2, padding=2),
            nn.BatchNorm2d(64),
            nn.ReLU(True),
            nn.Conv2d(64, 64, 5, stride=2, padding=2),
            nn.BatchNorm2d(64),
            nn.ReLU(True),
        )

        self.prenet = nn.Sequential(  # 预测图像深度
            nn.Conv2d(inputC + 64, inputC, 3, padding=1),
            nn.BatchNorm2d(inputC),
            nn.ReLU(True),
            nn.Conv2d(inputC, inputC, 3, padding=1),
            nn.BatchNorm2d(inputC),
            nn.ReLU(True)
        )
        self.depthnet = nn.Conv2d(inputC, self.D, 1)
        self.contextnet = nn.Conv2d(inputC, self.camC, 1)
        self.attention1 = nn.Conv2d(inputC, 1, 1)
        self.attention2 = nn.Conv2d(self.camC, 1, 1)

        self.convnet = nn.Sequential(  # 提取图像语义
            nn.Conv2d(inputC, inputC, 3, padding=1),
            nn.BatchNorm2d(inputC),
            nn.ReLU(True),
            nn.Conv2d(inputC, camC, 3, padding=1),
            nn.BatchNorm2d(camC),
            nn.ReLU(True),
        )

        self.upsample_depth = nn.Sequential(
            nn.ConvTranspose2d(inputC + 64, camC, kernel_size=5, padding=2, stride=2, output_padding=1, bias=False),
            nn.BatchNorm2d(camC),
            nn.ReLU(),
            nn.ConvTranspose2d(camC, camC, kernel_size=5, padding=2, stride=2, output_padding=1, bias=False),
            nn.BatchNorm2d(camC),
            nn.ReLU(),
            nn.ConvTranspose2d(camC, camC, kernel_size=5, padding=2, stride=2, output_padding=1, bias=False),
            nn.BatchNorm2d(camC),
            nn.ReLU(),
            nn.ConvTranspose2d(camC, camC, kernel_size=5, padding=2, bias=False),
            nn.BatchNorm2d(camC),
            nn.ReLU(),
            nn.Conv2d(camC, self.D, kernel_size=1),
        )


    def create_frustum(self):
        # make grid in image plane
        ogfH, ogfW = self.final_dim
        fH, fW = self.fH, self.fW
        ds = torch.arange(*self.grid_conf['dbound'], dtype=torch.float).view(-1, 1, 1).expand(-1, fH, fW)
        D, _, _ = ds.shape
        xs = torch.linspace(0, ogfW - 1, fW, dtype=torch.float).view(1, 1, fW).expand(D, fH, fW)
        ys = torch.linspace(0, ogfH - 1, fH, dtype=torch.float).view(1, fH, 1).expand(D, fH, fW)

        # D x H x W x 3
        frustum = torch.stack((xs, ys, ds), -1)
        return nn.Parameter(frustum, requires_grad=False)

    def get_geometry(self, rots, trans, post_rots=None, post_trans=None, extra_rots=None, extra_trans=None):
        """Determine the (x,y,z) locations (in the ego frame)
        of the points in the point cloud.
        Returns B x N x D x H/downsample x W/downsample x 3
        """
        B, N, _ = trans.shape
        # ADD
        # undo post-transformation
        # B x N x D x H x W x 3
        if post_rots is not None or post_trans is not None:
            if post_trans is not None:
                points = self.frustum - post_trans.view(B, N, 1, 1, 1, 3)
            if post_rots is not None:
                points = torch.inverse(post_rots).view(B, N, 1, 1, 1, 3, 3).matmul(points.unsqueeze(-1))
        else:
            points = self.frustum.repeat(B, N, 1, 1, 1, 1).unsqueeze(-1)  # B x N x D x H x W x 3 x 1

        # cam_to_ego
        points = torch.cat((points[:, :, :, :, :, :2] * points[:, :, :, :, :, 2:3],
                            points[:, :, :, :, :, 2:3]
                            ), 5)
        points = rots.view(B, N, 1, 1, 1, 3, 3).matmul(points).squeeze(-1)
        points += trans.view(B, N, 1, 1, 1, 3)

        if extra_rots is not None or extra_trans is not None:
            if extra_rots is not None:
                points = extra_rots.view(B, N, 1, 1, 1, 3, 3).matmul(points.unsqueeze(-1)).squeeze(-1)
            if extra_trans is not None:
                points += extra_trans.view(B, N, 1, 1, 1, 3)
        return points

    #lss_sup
    def get_cam_feats(self, x, d, metas):  # d : B x 6 x 5 x 896 x 1600 深度与四个方向梯度
        label_depth = d[:, :, :1]          # d : B x 6 x 5 x 896 x 1600
        metas[0].update({'label_depth': label_depth})
        B, N, C, fH, fW = x.shape          # fH : 112, fW : 200
        d = d.view(B * N, *d.shape[2:])    # B*N x 5 x 896 x 1600
        x = x.view(B * N, C, fH, fW)       # B*N x 256 x 112 x 200

        d = checkpoint(self.dtransform, d)
        # d = self.dtransform(d)             # B*N x 5 x 896 x 1600  -> B*N x 64 x 112 x 200 
        context = checkpoint(self.convnet, x)
        # context = self.convnet(x)          # B*N x 256 x 112 x 200 -> B*N x 64 x 112 x 200   提取语义特征
        x = torch.cat([x, d], dim=1)       # B*N x 256+64 x 112 x 200 
        
        depth_pre = checkpoint(self.upsample_depth, x)
        # depth_pre = self.upsample_depth(x)  # B*N x 256+64 x 112 x 200 -> B*N x D x 896 x 1600  增加x8上采样模块用来监督深度信息
        
        metas[0].update({'pre_upsample': depth_pre})  # B*N x D x 896 x 1600
        x = self.prenet(x) # B*N x 256+64 x 112 x 200 -> B*N x 256 x 112 x 200 # 合并深度和图像信息 cat好还是add好?  预测深度  [b*6, 128, 32, 88]  (用于计算loss)
        attention1 = self.attention1(x) # 256 -> B*N x 1 x 112 x 200
        depth = self.depthnet(x) * attention1  # B*N x D x 112 x 200
        metas[0].update({'pre_depth': depth})
        depth = depth.softmax(dim=1)

        #depth = depth
        x = self.contextnet(x) + context  # B*N x 256 x 112 x 200 -> B*N x 64 x 112 x 200
        attention2 = self.attention2(x)   # B*N x 64 x 112 x 200 -> B*N x 1 x 112 x 200
        x = depth.unsqueeze(1) * (x * attention2).unsqueeze(2) # B*N x 64 x D x 112 x 200
        x = x.view(B, N, self.camC, self.D, fH, fW)  # B x N x 64 x D x 112 x 200
        x = x.permute(0, 1, 3, 4, 5, 2)  # B x N x D x 112 x 200 x 64
        return x, depth, metas

    """
    def get_cam_feats(self, x):
        B, N, C, H, W = x.shape

        x = x.view(B * N, C, H, W)
        x, depth = self.camencode(x)
        x = x.view(B, N, self.camC, self.D, H, W)
        x = x.permute(0, 1, 3, 4, 5, 2)
        depth = depth.view(B, N, self.D, H, W)
        return x, depth
    """

    def voxel_pooling(self, geom_feats, x):
        B, N, D, H, W, C = x.shape
        Nprime = B * N * D * H * W
        batch_size = x.shape[0]
        # flatten x
        x = x.reshape(Nprime, C)

        # flatten indices
        geom_feats = ((geom_feats - (self.bx - self.dx / 2.)) / self.dx).long()  # 转成BEV视图
        geom_feats = geom_feats.view(Nprime, 3)
        batch_ix = torch.cat([torch.full([Nprime // B, 1], ix, device=x.device, dtype=torch.long) for ix in range(B)])
        batch_ix = batch_ix.to(geom_feats.device)
        geom_feats = torch.cat((geom_feats, batch_ix), 1)
        # filter out points that are outside box
        kept = (geom_feats[:, 0] >= 0) & (geom_feats[:, 0] < self.nx[0]) \
                & (geom_feats[:, 1] >= 0) & (geom_feats[:, 1] < self.nx[1]) \
                & (geom_feats[:, 2] >= 0) & (geom_feats[:, 2] < self.nx[2])
        x = x[kept]
        geom_feats = geom_feats[kept]
        # get tensors from the same voxel next to each other
        ranks = geom_feats[:, 0] * (self.nx[1] * self.nx[2] * B) \
                + geom_feats[:, 1] * (self.nx[2] * B) \
                + geom_feats[:, 2] * B \
                + geom_feats[:, 3]
        sorts = ranks.argsort()
        x, geom_feats, ranks = x[sorts], geom_feats[sorts], ranks[sorts]
        # cumsum trick
        if not self.use_quickcumsum:
            x, geom_feats = cumsum_trick(x, geom_feats, ranks)
        else:
            x, geom_feats = QuickCumsum.apply(x, geom_feats, ranks)

        # griddify (B x C x Z x X x Y)
        final = torch.zeros((B, C, self.nx[2], self.nx[0], self.nx[1]), device=x.device)
        final[geom_feats[:, 3], :, geom_feats[:, 2], geom_feats[:, 0], geom_feats[:, 1]] = x

        return final

    def get_voxels(self, x, rots=None, trans=None, post_rots=None, post_trans=None, extra_rots=None, extra_trans=None, depth_lidar=None, img_metas=None):
        geom = self.get_geometry(rots, trans, post_rots, post_trans, extra_rots, extra_trans)  # B x 6 x 41 x 112 x 200 x 3
        x, depth, img_metas = self.get_cam_feats(x, depth_lidar, img_metas)
        x = self.voxel_pooling(geom, x)
        return x, depth, img_metas

    def s2c(self, x):
        B, C, H, W, L = x.shape
        bev = torch.reshape(x, (B, C*H, W, L))
        bev = bev.permute((0,1,3,2))
        return bev

    def forward(self, x, rots, trans, lidar2img_rt=None, img_metas=None, post_rots=None, post_trans=None,
                extra_rots=None, extra_trans=None, depth_lidar=None):
        '''
        x : b x n x 256 x H x W
        
        '''
        
        x, depth, img_metas = self.get_voxels(x, rots, trans, post_rots, post_trans, extra_rots, extra_trans, depth_lidar, img_metas) # [B, C, H, W, L]  H是z轴的，此时是多层BEV； W,L是BEV平面上的轴
        bev = self.s2c(x) # B x C*H x H x L; B x 832 x 180 x 180
        x = self.bevencode(bev) # B x 256 x 180 x 180
        return x, depth, img_metas

