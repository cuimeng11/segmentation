from functools import reduce, lru_cache
from operator import mul
from torch.autograd import Variable, Function

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange
from timm.models.layers import DropPath, trunc_normal_


class ConvNormAct(nn.Module):

    def __init__(self, in_ch, out_ch, kernel_size=3, stride=1, padding=1, groups=1, dilation=1, act=True, bias=False):

        super().__init__()
        self.conv = nn.Conv3d(in_channels=in_ch, out_channels=out_ch, kernel_size=kernel_size, stride=stride, padding=padding, groups=groups, dilation=dilation, bias=bias)
        self.norm = nn.GroupNorm(num_groups=2, num_channels=out_ch)
        self.act = nn.ReLU() if act else nn.Identity()

    def forward(self, x):
        out = self.act(self.norm(self.conv(x)))
        return out


class BasicBlock(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=[3, 3, 3], stride=2, norm=nn.BatchNorm3d):
        super().__init__()

        pad_size = [i//2 for i in kernel_size]

        self.conv1 = ConvNormAct(in_ch, out_ch, kernel_size, stride=stride, padding=pad_size, )
        self.conv2 = ConvNormAct(out_ch, out_ch, kernel_size, stride=1, padding=pad_size)
        self.residual = ConvNormAct(in_ch, out_ch, kernel_size, stride=stride, padding=pad_size)

    def forward(self, x):
        shortcut = x
        x = self.conv1(x)
        x = self.conv2(x)
        x = x + self.residual(shortcut)
        return x


class Conv_Stem(nn.Module):
    def __init__(self, in_ch, out_ch, kernel_size=[3, 3, 3]):
        super().__init__()

        pad_size = [i // 2 for i in kernel_size]

        self.conv1 = BasicBlock(in_ch, out_ch // 2, kernel_size=kernel_size)
        self.conv2 = nn.Conv3d(out_ch // 2, out_ch, kernel_size=kernel_size, stride=2, padding=pad_size, bias=False)

    def forward(self, x):
        x_2 = self.conv1(x)
        x = self.conv2(x_2)
        return x


class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class CCA(nn.Module):  # channel cross attention
    def __init__(self, F_g, F_x):  # f_g是全局的特征通道数,f_x是输入x的特征通道数
        super().__init__()
        self.mlp_x = nn.Sequential(
            Flatten(),
            nn.Linear(F_x, F_x))
        self.mlp_g = nn.Sequential(
            Flatten(),
            nn.Linear(F_g, F_x))
        self.relu = nn.ReLU(inplace=True)

    def forward(self, g, x):
        avg_pool_x = F.avg_pool3d(x, (x.size(2), x.size(3), x.size(4)), stride=(x.size(2), x.size(3), x.size(4)))
        channel_att_x = self.mlp_x(avg_pool_x)
        avg_pool_g = F.avg_pool3d(g, (g.size(2), g.size(3), g.size(4)), stride=(g.size(2), g.size(3), g.size(4)))
        channel_att_g = self.mlp_g(avg_pool_g)
        channel_att_sum = (channel_att_x + channel_att_g) / 2.0
        scale = torch.sigmoid(channel_att_sum).unsqueeze(2).unsqueeze(3).unsqueeze(4).expand_as(x)
        x_after_channel = x * scale
        out = self.relu(x_after_channel)
        return out


class CoordAtt(nn.Module):  # 三维坐标注意力，对应解码器TCFC部分
    # 通过坐标信息生成的注意力权重来动态地调整输入张量中不同坐标维度上的特征响应，以更好地捕获不同位置的信息。
    def __init__(self, inp, oup, reduction=4):  # reduction是缩小比例，用于控制中间表示的通道数量
        super(CoordAtt, self).__init__()

        # 计算中间通道
        mip = inp // reduction

        self.conv1 = nn.Conv3d(inp, mip, kernel_size=1, stride=1, padding=0)
        self.bn1 = nn.BatchNorm3d(mip)
        self.relu1 = nn.ReLU()

        self.conv2 = nn.Conv3d(inp, mip, kernel_size=1, stride=1, padding=0)
        self.bn2 = nn.BatchNorm3d(mip)
        self.relu2 = nn.ReLU()
        #  conv_d, conv_h, conv_w 分别用于处理不同的坐标维度。
        self.conv_d = nn.Conv3d(mip, oup, kernel_size=1, stride=1, padding=0)
        self.conv_h = nn.Conv3d(mip, oup, kernel_size=1, stride=1, padding=0)
        self.conv_w = nn.Conv3d(mip, oup, kernel_size=1, stride=1, padding=0)

    def forward(self, g, x):
        b, c, d, h, w = x.size()
        # 沿每个维度对 g 进行全局平均汇集
        g_d = F.adaptive_avg_pool3d(g, (d, 1, 1))
        # 计算 g 中每个深度切片（沿深度维度的二维平面）的平均值，并输出一个深度为 d，高度和宽度均为 1 的三维张量。
        g_h = F.adaptive_avg_pool3d(g, (1, h, 1)).permute(0, 1, 3, 2, 4)
        g_w = F.adaptive_avg_pool3d(g, (1, 1, w)).permute(0, 1, 4, 2, 3)
        # 沿每个维度对 x 进行全局平均汇集
        x_d = F.adaptive_avg_pool3d(x, (d, 1, 1))
        x_h = F.adaptive_avg_pool3d(x, (1, h, 1)).permute(0, 1, 3, 2, 4)
        x_w = F.adaptive_avg_pool3d(x, (1, 1, w)).permute(0, 1, 4, 2, 3)
        # 沿每个维度将全局平均池化特征串联起来
        g_y = torch.cat([g_d, g_h, g_w], dim=2)
        g_y = self.conv1(g_y)
        g_y = self.bn1(g_y)
        g_y = self.relu1(g_y)

        x_y = torch.cat([x_d, x_h, x_w], dim=2)
        x_y = self.conv2(x_y)
        x_y = self.bn2(x_y)
        x_y = self.relu2(x_y)
        # 将连接的特征拆分成不同的维度
        g_d, g_h, g_w = torch.split(g_y, [d, h, w], dim=2)
        g_h = g_h.permute(0, 1, 3, 2, 4)
        g_w = g_w.permute(0, 1, 3, 4, 2)

        x_d, x_h, x_w = torch.split(x_y, [d, h, w], dim=2)
        x_h = x_h.permute(0, 1, 3, 2, 4)
        x_w = x_w.permute(0, 1, 3, 4, 2)
        # 计算注意力权重
        a_d = (x_d + g_d) / 2
        a_h = (x_h + g_h) / 2
        a_w = (x_w + g_w) / 2

        a_d, a_h, a_w = torch.sigmoid(self.conv_d(a_d)), torch.sigmoid(self.conv_h(a_h)), torch.sigmoid(
            self.conv_w(a_w))

        x = x * a_d * a_h * a_w
        return x


class SEBlock(nn.Module):
    def __init__(self, in_ch, ratio=4, act=nn.ReLU):
        super().__init__()

        self.squeeze = nn.AdaptiveAvgPool3d(1)
        self.excitation = nn.Sequential(
            nn.Conv3d(in_ch, in_ch // ratio, kernel_size=1),
            act(),
            nn.Conv3d(in_ch // ratio, in_ch, kernel_size=1),
            nn.Sigmoid()
        )

    def forward(self, x):
        out = self.squeeze(x)
        out = self.excitation(out)

        return x * out


class MBConv(nn.Module):
    def __init__(self, in_ch, out_ch, expansion=4, kernel_size=3, stride=1, ratio=4, se=True):
        super().__init__()

        padding = (kernel_size - 1) // 2
        expanded = expansion * in_ch
        self.se = se

        self.expand_proj = nn.Identity() if (expansion == 1) else ConvNormAct(in_ch, expanded, kernel_size=1, padding=0)
        self.depthwise = ConvNormAct(expanded, expanded, kernel_size=kernel_size, stride=stride, padding=padding,
                                     groups=expanded)

        if self.se:
            self.se = SEBlock(expanded, ratio=ratio)

        self.pointwise = ConvNormAct(expanded, out_ch, kernel_size=1, padding=0, act=False)

    def forward(self, x):
        x = x.permute(0, 4, 1, 2, 3)
        x = self.expand_proj(x)
        x = self.depthwise(x)
        if self.se:
            x = self.se(x)
        x = self.pointwise(x)
        x = x.permute(0, 2, 3, 4, 1)
        return x


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


def window_partition(x, window_size):
    # 在输入张量的每个维度上进行划窗操作，产生了一个新的张量windows，其中包含了按窗口大小切割后的子张量。
    B, D, H, W, C = x.shape
    x = x.view(B, D // window_size[0], window_size[0], H // window_size[1], window_size[1], W // window_size[2],
               window_size[2], C)
    windows = x.permute(0, 1, 3, 5, 2, 4, 6, 7).contiguous().view(-1, reduce(mul, window_size), C)
    # (batch_size * depth_windows * height_windows * width_windows, window_size[0] * window_size[1] * window_size[2], channels)
    return windows


def window_reverse(windows, window_size, B, D, H, W):
    # 划窗操作的逆过程，用于将划窗得到的张量 windows 还原回原始形状。
    x = windows.view(B, D // window_size[0], H // window_size[1], W // window_size[2], window_size[0], window_size[1],
                     window_size[2], -1)
    x = x.permute(0, 1, 4, 2, 5, 3, 6, 7).contiguous().view(B, D, H, W, -1)
    return x


def get_window_size(x_size, window_size, shift_size=None):
    # 计算最终使用的windows大小和步幅。
    # 确保在划窗操作中使用的窗口大小和步幅不会导致超出输入张量的边界。
    use_window_size = list(window_size)
    if shift_size is not None:
        use_shift_size = list(shift_size)
    for i in range(len(x_size)):
        # 如果输入尺寸小于等于窗口尺寸，使用输入尺寸，并将步幅设为0
        if x_size[i] <= window_size[i]:
            use_window_size[i] = x_size[i]
            if shift_size is not None:
                use_shift_size[i] = 0

    if shift_size is None:
        return tuple(use_window_size)
    else:
        return tuple(use_window_size), tuple(use_shift_size)


class CrossWindowAttention3D(nn.Module):
    def __init__(self, dim, window_size, num_heads, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.dim = dim
        self.window_size = window_size  # Wd, Wh, Ww
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1) * (2 * window_size[2] - 1), num_heads))
        # 相对位置偏置表，形状为:2*Wd-1 * 2*Wh-1 * 2*Ww-1, nH

        # 构建相对位置索引和偏置表
        coords_d = torch.arange(self.window_size[0])
        coords_h = torch.arange(self.window_size[1])
        coords_w = torch.arange(self.window_size[2])
        coords = torch.stack(torch.meshgrid(coords_d, coords_h, coords_w))  # 3, Wd, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 3, Wd*Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 3, Wd*Wh*Ww, Wd*Wh*Ww
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wd*Wh*Ww, Wd*Wh*Ww, 3
        relative_coords[:, :, 0] += self.window_size[0] - 1  # shift to start from 0
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 2] += self.window_size[2] - 1

        relative_coords[:, :, 0] *= (2 * self.window_size[1] - 1) * (2 * self.window_size[2] - 1)
        relative_coords[:, :, 1] *= (2 * self.window_size[2] - 1)
        relative_position_index = relative_coords.sum(-1)  # Wd*Wh*Ww, Wd*Wh*Ww
        self.register_buffer("relative_position_index", relative_position_index)
        self.query = nn.Linear(dim, dim)
        self.key = nn.Linear(dim, dim)
        self.value = nn.Linear(dim, dim)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        # 相对位置偏置表的初始化
        trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, y, mask=None):
        B_, N, C = x.shape
        q, k, v = self.query(x).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3), self.key(
            y).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3), self.value(y).reshape(B_, N,
                                                                                                              self.num_heads,
                                                                                                              C // self.num_heads).permute(
            0, 2, 1, 3)
        q = q * self.scale
        attn = q @ k.transpose(-2, -1)

        relative_position_bias = self.relative_position_bias_table[
            self.relative_position_index[:N, :N].reshape(-1)].reshape(
            N, N, -1)  # Wd*Wh*Ww,Wd*Wh*Ww,nH
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wd*Wh*Ww, Wd*Wh*Ww
        attn = attn + relative_position_bias.unsqueeze(0)  # B_, nH, N, N

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class SelfWindowAttention3D(nn.Module):

    def __init__(self, dim, window_size, num_heads, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):

        super().__init__()
        self.dim = dim
        self.window_size = window_size  # Wd, Wh, Ww
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1) * (2 * window_size[2] - 1), num_heads))
        # 2*Wd-1 * 2*Wh-1 * 2*Ww-1, nH

        coords_d = torch.arange(self.window_size[0])
        coords_h = torch.arange(self.window_size[1])
        coords_w = torch.arange(self.window_size[2])
        coords = torch.stack(torch.meshgrid(coords_d, coords_h, coords_w))  # 3, Wd, Wh, Ww
        coords_flatten = torch.flatten(coords, 1)  # 3, Wd*Wh*Ww
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]  # 3, Wd*Wh*Ww, Wd*Wh*Ww
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()  # Wd*Wh*Ww, Wd*Wh*Ww, 3
        relative_coords[:, :, 0] += self.window_size[0] - 1
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 2] += self.window_size[2] - 1

        relative_coords[:, :, 0] *= (2 * self.window_size[1] - 1) * (2 * self.window_size[2] - 1)
        relative_coords[:, :, 1] *= (2 * self.window_size[2] - 1)
        relative_position_index = relative_coords.sum(-1)  # Wd*Wh*Ww, Wd*Wh*Ww
        self.register_buffer("relative_position_index", relative_position_index)
        self.num_attention_heads = num_heads
        self.attention_head_size = int(dim / num_heads)
        self.all_head_size = self.num_attention_heads * self.attention_head_size
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x, mask=None):

        B_, N, C = x.shape
        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]  # B_, nH, N, C

        q = q * self.scale
        attn = q @ k.transpose(-2, -1)

        relative_position_bias = self.relative_position_bias_table[
            self.relative_position_index[:N, :N].reshape(-1)].reshape(N, N, -1)
        # Wd*Wh*Ww,Wd*Wh*Ww,nH
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()  # nH, Wd*Wh*Ww, Wd*Wh*Ww
        attn = attn + relative_position_bias.unsqueeze(0)  # B_, nH, N, N

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class PositionalEncoding3D(nn.Module):

    def __init__(self, channels):
        super(PositionalEncoding3D, self).__init__()
        # 确保通道数是偶数
        channels = int(np.ceil(channels / 6) * 2)  # ceil向上取整
        if channels % 2:
            channels += 1
        self.channels = channels
        # 频率递减的序列，用于后续的正弦和余弦函数计算
        inv_freq = 1. / (10000 ** (torch.arange(0, channels, 2).float() / channels))
        self.register_buffer('inv_freq', inv_freq)

    def forward(self, tensor):
        if len(tensor.shape) != 5:
            raise RuntimeError("The input tensor has to be 5d!")
        batch_size, x, y, z, orig_ch = tensor.shape
        # 使用正弦和余弦函数，计算了三个方向上的位置编码
        pos_x = torch.arange(x, device=tensor.device).type(self.inv_freq.type())
        pos_y = torch.arange(y, device=tensor.device).type(self.inv_freq.type())
        pos_z = torch.arange(z, device=tensor.device).type(self.inv_freq.type())
        sin_inp_x = torch.einsum("i,j->ij", pos_x, self.inv_freq)
        sin_inp_y = torch.einsum("i,j->ij", pos_y, self.inv_freq)
        sin_inp_z = torch.einsum("i,j->ij", pos_z, self.inv_freq)
        # 将正弦和余弦函数的结果拼接成新的张量
        emb_x = torch.cat((sin_inp_x.sin(), sin_inp_x.cos()), dim=-1).unsqueeze(1).unsqueeze(1)
        emb_y = torch.cat((sin_inp_y.sin(), sin_inp_y.cos()), dim=-1).unsqueeze(1)
        emb_z = torch.cat((sin_inp_z.sin(), sin_inp_z.cos()), dim=-1)
        # 创建一个全零的张量emb，将计算得到的位置编码张量填充到其中。
        emb = torch.zeros((x, y, z, self.channels * 3), device=tensor.device).type(tensor.type())
        emb[:, :, :, :self.channels] = emb_x
        emb[:, :, :, self.channels:2 * self.channels] = emb_y
        emb[:, :, :, 2 * self.channels:] = emb_z
        # 将计算得到的位置编码张量重复扩展到 batch 维度上，并将通道数调整为 orig_ch。这个位置编码张量将会和输入张量相加，以提供相对位置信息。
        return emb[None, :, :, :, :orig_ch].repeat(batch_size, 1, 1, 1, 1)


class SwinTransformerBlock3D(nn.Module):

    def __init__(self, dim=64, num_heads=8, window_size=(7, 7, 7), shift_size=(0, 0, 0), mlp_ratio=4.,
                 qkv_bias=True, qk_scale=None, drop=0., attn_drop=0., drop_path=0., act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio

        assert 0 <= self.shift_size[0] < self.window_size[0], "shift_size must in 0-window_size"
        assert 0 <= self.shift_size[1] < self.window_size[1], "shift_size must in 0-window_size"
        assert 0 <= self.shift_size[2] < self.window_size[2], "shift_size must in 0-window_size"

        self.norm_t1_1 = norm_layer(dim)
        self.norm_t1ce_1 = norm_layer(dim)
        self.norm_t2_1 = norm_layer(dim)
        self.norm_flair_1 = norm_layer(dim)
        self.self_attn_t1 = SelfWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.self_attn_t1ce = SelfWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.self_attn_t2 = SelfWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.self_attn_flair = SelfWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.cross_attn_1 = CrossWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.cross_attn_2 = CrossWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm_t1_2 = norm_layer(dim)
        self.norm_t1ce_2 = norm_layer(dim)
        self.norm_t2_2 = norm_layer(dim)
        self.norm_flair_2 = norm_layer(dim)
        self.mlp_t1 = MBConv(in_ch=dim, out_ch=dim)
        self.mlp_t1ce = MBConv(in_ch=dim, out_ch=dim)
        self.mlp_t2 = MBConv(in_ch=dim, out_ch=dim)
        self.mlp_flair = MBConv(in_ch=dim, out_ch=dim)

    def forward_part1(self, t1, t1ce, t2, flair, mask_matrix, cross):
        B, D, H, W, C = t1.shape
        window_size, shift_size = get_window_size((D, H, W), self.window_size, self.shift_size)

        t1, t1ce, t2, flair = self.norm_t1_1(t1), self.norm_t1ce_1(t1ce), self.norm_t2_1(t2), self.norm_flair_1(flair),
        # pad feature maps to multiples of window size
        pad_l = pad_t = pad_d0 = 0
        pad_d1 = (window_size[0] - D % window_size[0]) % window_size[0]
        pad_b = (window_size[1] - H % window_size[1]) % window_size[1]
        pad_r = (window_size[2] - W % window_size[2]) % window_size[2]
        t1 = F.pad(t1, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        t1ce = F.pad(t1ce, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        t2 = F.pad(t2, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        flair = F.pad(flair, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        _, Dp, Hp, Wp, _ = t1.shape
        # cyclic shift
        if any(i > 0 for i in shift_size):
            shifted_t1 = torch.roll(t1, shifts=(-shift_size[0], -shift_size[1], -shift_size[2]), dims=(1, 2, 3))
            shifted_t1ce = torch.roll(t1ce, shifts=(-shift_size[0], -shift_size[1], -shift_size[2]), dims=(1, 2, 3))
            shifted_t2 = torch.roll(t2, shifts=(-shift_size[0], -shift_size[1], -shift_size[2]), dims=(1, 2, 3))
            shifted_flair = torch.roll(flair, shifts=(-shift_size[0], -shift_size[1], -shift_size[2]), dims=(1, 2, 3))
            attn_mask = mask_matrix
        else:
            shifted_t1 = t1
            shifted_t1ce = t1ce
            shifted_t2 = t2
            shifted_flair = flair
            attn_mask = None
        # partition windows
        t1_windows = window_partition(shifted_t1, window_size)  # B*nW, Wd*Wh*Ww, C
        t1ce_windows = window_partition(shifted_t1ce, window_size)
        t2_windows = window_partition(shifted_t2, window_size)
        flair_windows = window_partition(shifted_flair, window_size)
        # W-MSA/SW-MSA
        attn_windows_t1 = self.self_attn_t1(t1_windows, mask=attn_mask) if cross == False else self.self_attn_t1(
            t1_windows, mask=attn_mask) + self.cross_attn_1(t1_windows, t1ce_windows, mask=attn_mask)
        attn_windows_t1ce = self.self_attn_t1ce(t1ce_windows,
                                                mask=attn_mask) if cross == False else self.self_attn_t1ce(t1ce_windows,
                                                                                                           mask=attn_mask) + self.cross_attn_1(
            t1ce_windows, t1_windows, mask=attn_mask)
        attn_windows_t2 = self.self_attn_t2(t2_windows, mask=attn_mask) if cross == False else self.self_attn_t2(
            t2_windows, mask=attn_mask) + self.cross_attn_2(t2_windows, flair_windows, mask=attn_mask)
        attn_windows_flair = self.self_attn_flair(flair_windows,
                                                  mask=attn_mask) if cross == False else self.self_attn_flair(
            flair_windows, mask=attn_mask) + self.cross_attn_2(flair_windows, t2_windows, mask=attn_mask)
        # merge windows
        attn_windows_t1 = attn_windows_t1.view(-1, *(window_size + (C,)))
        attn_windows_t1ce = attn_windows_t1ce.view(-1, *(window_size + (C,)))
        attn_windows_t2 = attn_windows_t2.view(-1, *(window_size + (C,)))
        attn_windows_flair = attn_windows_flair.view(-1, *(window_size + (C,)))
        shifted_t1 = window_reverse(attn_windows_t1, window_size, B, Dp, Hp, Wp)
        shifted_t1ce = window_reverse(attn_windows_t1ce, window_size, B, Dp, Hp, Wp)
        shifted_t2 = window_reverse(attn_windows_t2, window_size, B, Dp, Hp, Wp)
        shifted_flair = window_reverse(attn_windows_flair, window_size, B, Dp, Hp, Wp)
        # reverse cyclic shift
        if any(i > 0 for i in shift_size):
            t1 = torch.roll(shifted_t1, shifts=(shift_size[0], shift_size[1], shift_size[2]), dims=(1, 2, 3))
            t1ce = torch.roll(shifted_t1ce, shifts=(shift_size[0], shift_size[1], shift_size[2]), dims=(1, 2, 3))
            t2 = torch.roll(shifted_t2, shifts=(shift_size[0], shift_size[1], shift_size[2]), dims=(1, 2, 3))
            flair = torch.roll(shifted_flair, shifts=(shift_size[0], shift_size[1], shift_size[2]), dims=(1, 2, 3))
        else:
            t1 = shifted_t1
            t1ce = shifted_t1ce
            t2 = shifted_t2
            flair = shifted_flair

        if pad_d1 > 0 or pad_r > 0 or pad_b > 0:
            t1 = t1[:, :D, :H, :W, :].contiguous()
            t1ce = t1ce[:, :D, :H, :W, :].contiguous()
            t2 = t2[:, :D, :H, :W, :].contiguous()
            flair = flair[:, :D, :H, :W, :].contiguous()
        return t1, t1ce, t2, flair

    def forward_part2(self, t1, t1ce, t2, flair):

        t1 = self.drop_path(self.mlp_t1(self.norm_t1_2(t1)))
        t1ce = self.drop_path(self.mlp_t1ce(self.norm_t1ce_2(t1ce)))
        t2 = self.drop_path(self.mlp_t2(self.norm_t2_2(t2)))
        flair = self.drop_path(self.mlp_flair(self.norm_flair_2(flair)))
        return t1, t1ce, t2, flair

    def forward(self, t1, t1ce, t2, flair, mask_matrix, cross):
        t1_shortcut, t1ce_shortcut, t2_shortcut, flair_shortcut = t1, t1ce, t2, flair
        t1, t1ce, t2, flair = self.forward_part1(t1, t1ce, t2, flair, mask_matrix, cross)
        t1 = t1_shortcut + self.drop_path(t1)
        t1ce = t1ce_shortcut + self.drop_path(t1ce)
        t2 = t2_shortcut + self.drop_path(t2)
        flair = flair_shortcut + self.drop_path(flair)

        t1_shortcut, t1ce_shortcut, t2_shortcut, flair_shortcut = t1, t1ce, t2, flair
        t1, t1ce, t2, flair = self.forward_part2(t1, t1ce, t2, flair)
        t1, t1ce, t2, flair = t1_shortcut + t1, t1ce_shortcut + t1ce, t2_shortcut + t2, flair_shortcut + flair

        return t1, t1ce, t2, flair


class BottleneckBlock3D(nn.Module):
    # just a swin transformer???
    def __init__(self, dim, num_heads=8, window_size=(7, 7, 7), shift_size=(0, 0, 0), mlp_ratio=4., qkv_bias=True,
                 qk_scale=None, drop=0., attn_drop=0., drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio

        assert 0 <= self.shift_size[0] < self.window_size[0], "shift_size must in 0-window_size"
        assert 0 <= self.shift_size[1] < self.window_size[1], "shift_size must in 0-window_size"
        assert 0 <= self.shift_size[2] < self.window_size[2], "shift_size must in 0-window_size"

        self.norm_1 = norm_layer(dim)
        self.self_attn_x = SelfWindowAttention3D(
            dim, window_size=self.window_size, num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm_2 = norm_layer(dim)
        self.mlp = MBConv(in_ch=dim, out_ch=dim)

    def forward_part1(self, x, mask_matrix, cross):
        B, D, H, W, C = x.shape
        window_size, shift_size = get_window_size((D, H, W), self.window_size, self.shift_size)

        x = self.norm_1(x)
        # pad feature maps to multiples of window size
        pad_l = pad_t = pad_d0 = 0
        pad_d1 = (window_size[0] - D % window_size[0]) % window_size[0]
        pad_b = (window_size[1] - H % window_size[1]) % window_size[1]
        pad_r = (window_size[2] - W % window_size[2]) % window_size[2]
        x = F.pad(x, (0, 0, pad_l, pad_r, pad_t, pad_b, pad_d0, pad_d1))
        _, Dp, Hp, Wp, _ = x.shape
        # cyclic shift
        if any(i > 0 for i in shift_size):
            shifted_x = torch.roll(x, shifts=(-shift_size[0], -shift_size[1], -shift_size[2]), dims=(1, 2, 3))
            attn_mask = mask_matrix
        else:
            shifted_x = x
            attn_mask = None
        # partition windows
        x_windows = window_partition(shifted_x, window_size)  # B*nW, Wd*Wh*Ww, C

        # W-MSA/SW-MSA
        attn_windows_x = self.self_attn_x(x_windows, mask=attn_mask)
        # merge window
        attn_windows_x = attn_windows_x.view(-1, *(window_size + (C,)))
        shifted_x = window_reverse(attn_windows_x, window_size, B, Dp, Hp, Wp)  # B D' H' W' C=
        # reverse cyclic shift
        if any(i > 0 for i in shift_size):
            x = torch.roll(shifted_x, shifts=(shift_size[0], shift_size[1], shift_size[2]), dims=(1, 2, 3))

        else:
            x = shifted_x

        if pad_d1 > 0 or pad_r > 0 or pad_b > 0:
            x = x[:, :D, :H, :W, :].contiguous()

        return x

    def forward_part2(self, x):

        x = self.drop_path(self.mlp(self.norm_2(x)))
        return x

    def forward(self, x, mask_matrix):
        x_shortcut = x
        x = self.forward_part1(x, mask_matrix, cross=False)
        x = x_shortcut + self.drop_path(x)

        x_shortcut = x
        x = self.forward_part2(x)
        x = x_shortcut + x

        return x


class PatchMerging(nn.Module):

    def __init__(self, dim, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.reduction = nn.Conv3d(dim, dim * 2, kernel_size=3, stride=2, padding=1)

        self.norm = norm_layer(dim)

    def forward(self, x):
        x = F.gelu(x)
        x = self.norm(x)
        x = x.permute(0, 4, 1, 2, 3).contiguous()
        x = self.reduction(x)
        x = x.permute(0, 2, 3, 4, 1).contiguous()
        return x


@lru_cache()
def compute_mask(D, H, W, window_size, shift_size, device):
    img_mask = torch.zeros((1, D, H, W, 1), device=device)  # 1 Dp Hp Wp 1
    cnt = 0
    for d in slice(-window_size[0]), slice(-window_size[0], -shift_size[0]), slice(-shift_size[0], None):
        for h in slice(-window_size[1]), slice(-window_size[1], -shift_size[1]), slice(-shift_size[1], None):
            for w in slice(-window_size[2]), slice(-window_size[2], -shift_size[2]), slice(-shift_size[2], None):
                img_mask[:, d, h, w, :] = cnt
                cnt += 1
    mask_windows = window_partition(img_mask, window_size)  # nW, ws[0]*ws[1]*ws[2], 1
    mask_windows = mask_windows.squeeze(-1)  # nW, ws[0]*ws[1]*ws[2]
    attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
    attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))
    return attn_mask


class BasicLayer(nn.Module):

    def __init__(self, dim, depth, depths, num_heads, window_size=(7, 7, 7), mlp_ratio=4., qkv_bias=False,
                 qk_scale=None, drop=0., attn_drop=0., drop_path=0., drop_path_rate=0., norm_layer=nn.LayerNorm,
                 downsample=None):
        super().__init__()
        self.window_size = window_size
        self.shift_size = tuple(i // 2 for i in window_size)
        self.depth = depth

        self.blocks = nn.ModuleList([
            SwinTransformerBlock3D(
                dim=dim, num_heads=num_heads, window_size=window_size,
                shift_size=(0, 0, 0) if (i % 2 == 0) else self.shift_size,
                mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop, attn_drop=attn_drop,
                drop_path=drop_path[i] if isinstance(drop_path, list) else drop_path, norm_layer=norm_layer)
            for i in range(depth)])

        self.downsample = downsample
        if self.downsample is not None:
            self.downsample = downsample(dim=dim, norm_layer=norm_layer)

    def forward(self, t1, t1ce, t2, flair, extract_feature):

        B, C, D, H, W = t1.shape
        window_size, shift_size = get_window_size((D, H, W), self.window_size, self.shift_size)
        t1, t1ce, t2, flair = rearrange(t1, 'b c d h w -> b d h w c'), rearrange(t1ce,
                                                                                 'b c d h w -> b d h w c'), rearrange(
            t2, 'b c d h w -> b d h w c'), rearrange(flair, 'b c d h w -> b d h w c')

        Dp = int(np.ceil(D / window_size[0])) * window_size[0]
        Hp = int(np.ceil(H / window_size[1])) * window_size[1]
        Wp = int(np.ceil(W / window_size[2])) * window_size[2]
        attn_mask = compute_mask(Dp, Hp, Wp, window_size, shift_size, t1.device)

        for depth, blk in enumerate(self.blocks):
            t1_t1ce = torch.cat((t1, t1ce), dim=1)
            t1_t2 = torch.cat((t1, t2), dim=1)
            t1_flair = torch.cat((t1, flair), dim=1)
            extract_feature, t1_t1ce, t1_t2, t1_flair = blk(t1_t1ce, t1_t2, t1_flair, attn_mask,
                                                            cross=True if depth == len(self.blocks) - 1 else False)
        # for depth, blk in enumerate(self.blocks):
        #     t1, t1ce, t2, flair = blk(t1, t1ce, t2, flair, attn_mask,
        #                               cross=True if depth == len(self.blocks) - 1 else False)
        extract_feature.append(torch.cat(
            [rearrange(t1, 'b d h w c -> b c d h w'), rearrange(t1ce, 'b d h w c -> b c d h w'),
             rearrange(t2, 'b d h w c -> b c d h w'), rearrange(flair, 'b d h w c -> b c d h w')], dim=1))
        t1, t1ce, t2, flair = t1.reshape(B, D, H, W, -1), t1ce.reshape(B, D, H, W, -1), t2.reshape(B, D, H, W, -1), flair.reshape(B, D, H, W, -1)

        if self.downsample is not None:
            t1, t1ce, t2, flair = self.downsample(t1), self.downsample(t1ce), self.downsample(t2), self.downsample(
                flair)
        t1, t1ce, t2, flair = rearrange(t1, 'b d h w c -> b c d h w'), rearrange(t1ce,
                                                                                 'b d h w c -> b c d h w'), rearrange(
            t2, 'b d h w c -> b c d h w'), rearrange(flair, 'b d h w c -> b c d h w')

        return extract_feature, t1, t1ce, t2, flair


class PatchEmbed3D(nn.Module):

    def __init__(self, img_size=(128, 128, 128), patch_size=(4, 4, 4), in_chans=1, embed_dim=64, norm_layer=None):
        super().__init__()

        self.patch_size = patch_size
        self.in_chans = in_chans
        self.embed_dim = embed_dim
        self.patches_resolution = [img_size[0] // patch_size[0], img_size[1] // patch_size[1], img_size[1] // patch_size[1]]

        # self.proj = nn.Conv3d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size) big kernel convolution
        self.proj = Conv_Stem(in_chans, embed_dim)
        if norm_layer is not None:
            self.norm = norm_layer(embed_dim)
        else:
            self.norm = None

    def forward(self, x):

        _, _, D, H, W = x.shape
        if W % self.patch_size[2] != 0:
            x = F.pad(x, (0, self.patch_size[2] - W % self.patch_size[2]))
        if H % self.patch_size[1] != 0:
            x = F.pad(x, (0, 0, 0, self.patch_size[1] - H % self.patch_size[1]))
        if D % self.patch_size[0] != 0:
            x = F.pad(x, (0, 0, 0, 0, 0, self.patch_size[0] - D % self.patch_size[0]))

        x_2, x = self.proj(x)
        if self.norm is not None:
            D, H, W = x.size(2), x.size(3), x.size(4)
            x = x.flatten(2).transpose(1, 2)
            x = self.norm(x)
            x = x.transpose(1, 2).view(-1, self.embed_dim, D, H, W)
        return x_2, x


class Encoder(nn.Module):

    def __init__(self, embed_dim=32, img_size=(128, 128, 128), patch_size=(4, 4, 4), in_chans=1,
                 depths=[2, 2, 2], num_heads=[2, 4, 8, 16], window_size=(7, 7, 7), mlp_ratio=4.,
                 qkv_bias=True, qk_scale=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1,
                 norm_layer=nn.LayerNorm,
                 patch_norm=True
                 ):
        super().__init__()

        self.num_layers = len(depths)
        self.embed_dim = embed_dim
        self.patch_norm = patch_norm
        self.num_features = int(embed_dim * 2 ** (self.num_layers - 1))
        self.mlp_ratio = mlp_ratio
        self.window_size = window_size
        # split image into non-overlapping patches
        self.patch_embed_t1 = PatchEmbed3D(img_size=img_size, patch_size=patch_size, in_chans=in_chans,
                                           embed_dim=embed_dim, norm_layer=norm_layer if self.patch_norm else None)
        self.patch_embed_t1ce = PatchEmbed3D(img_size=img_size, patch_size=patch_size, in_chans=in_chans,
                                             embed_dim=embed_dim, norm_layer=norm_layer if self.patch_norm else None)
        self.patch_embed_t2 = PatchEmbed3D(img_size=img_size, patch_size=patch_size, in_chans=in_chans,
                                           embed_dim=embed_dim, norm_layer=norm_layer if self.patch_norm else None)
        self.patch_embed_flair = PatchEmbed3D(img_size=img_size, patch_size=patch_size, in_chans=in_chans,
                                              embed_dim=embed_dim, norm_layer=norm_layer if self.patch_norm else None)

        self.patches_resolution = self.patch_embed_t1.patches_resolution

        self.pos_drop = nn.Dropout(p=drop_rate)

        # stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]  # stochastic depth decay rule

        # build encoder and bottleneck layers
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            layer = BasicLayer(dim=int(embed_dim * 2 ** i_layer), depth=depths[i_layer], depths=depths,
                               num_heads=num_heads[i_layer],
                               window_size=window_size, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                               drop=drop_rate, attn_drop=attn_drop_rate,
                               drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                               drop_path_rate=drop_path_rate, norm_layer=norm_layer, downsample=PatchMerging)
            self.layers.append(layer)

        self.bottleneck = nn.ModuleList([
            BottleneckBlock3D(
                dim=(embed_dim * 2 ** (i_layer + 1)) * 4, num_heads=num_heads[i_layer + 1], window_size=window_size,
                shift_size=(0, 0, 0),
                mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate,
                norm_layer=norm_layer),
            BottleneckBlock3D(
                dim=(embed_dim * 2 ** (i_layer + 1)) * 4, num_heads=num_heads[i_layer + 1], window_size=window_size,
                shift_size=tuple(i // 2 for i in window_size),
                mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale, drop=drop_rate, attn_drop=attn_drop_rate,
                norm_layer=norm_layer
            )
        ])
        self.norm = norm_layer((embed_dim * 2 ** (i_layer + 1)) * 4)

    def forward(self, t1, t1ce, t2, flair):
        extract_feature = []
        t1_2, t1 = self.patch_embed_t1(t1)
        t1ce_2, t1ce = self.patch_embed_t1ce(t1ce)
        t2_2, t2 = self.patch_embed_t2(t2)
        flair_2, flair = self.patch_embed_flair(flair)
        t1, t1ce, t2, flair = self.pos_drop(t1), self.pos_drop(t1ce), self.pos_drop(t2), self.pos_drop(flair)

        for i, layer in enumerate(self.layers):
            extract_feature, t1, t1ce, t2, flair = layer(t1, t1ce, t2, flair, extract_feature)
        x = torch.cat([t1, t1ce, t2, flair], dim=1)
        B, C, D, H, W = x.shape
        shift_size = tuple(i // 2 for i in self.window_size)
        window_size, shift_size = get_window_size((D, H, W), self.window_size, shift_size)

        Dp = int(np.ceil(D / window_size[0])) * window_size[0]
        Hp = int(np.ceil(H / window_size[1])) * window_size[1]
        Wp = int(np.ceil(W / window_size[2])) * window_size[2]
        attn_mask = compute_mask(Dp, Hp, Wp, window_size, shift_size, x.device)
        x = rearrange(x, 'n c d h w -> n d h w c')
        for i, layer in enumerate(self.bottleneck):
            x = layer(x, attn_mask)
        x = self.norm(x)
        x = rearrange(x, 'n d h w c -> n c d h w')

        return torch.cat([t1_2, t1ce_2, t2_2, flair_2], dim=1), extract_feature[0], extract_feature[1], extract_feature[2], x


class extract(nn.Module):
        def __init__(self, embed_dim, output_dim, img_size, patch_size, in_chans, depths, num_heads, window_size,
                     mlp_ratio):
            super().__init__()
            self.encoder = Encoder(embed_dim=embed_dim, img_size=img_size, patch_size=patch_size, in_chans=in_chans,
                                   depths=depths, num_heads=num_heads, window_size=window_size, mlp_ratio=mlp_ratio)

        def forward(self, inputs):
            t1, t1ce, t2, flair = inputs[:, 0, :, :, :].unsqueeze(1), inputs[:, 1, :, :, :].unsqueeze(1), inputs[:, 2, :
            , :, :].unsqueeze(1), inputs[:, 3, :, :, :].unsqueeze(1)
            z1, z2, z3, z4, z5 = self.encoder(t1, t1ce, t2, flair)
            z0 = torch.cat([t1, t1ce, t2, flair], dim=1)
            return z0, z1, z2, z3, z4, z5


class SpacialAttention3D(nn. Module):
    def __init__(self, kernel_size=7):
        super(SpacialAttention3D, self).__init__()
        padding = kernel_size // 2
        self.conv = nn.Conv3d(2, 1, kernel_size, 1, padding, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        max_pool_out,_ = torch.max(x, dim=1, keepdim=True)
        mean_pool_out = torch.mean(x, dim=1, keepdim=True)
        pool_out = torch.cat([max_pool_out, mean_pool_out], dim=1)
        out = self.conv(pool_out)
        out = self.sigmoid(out)

        return out


# MS-ADC
class AttDeformConv3d(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=False, se_ratio=4):
        super(AttDeformConv3d, self).__init__()
        self.kernel_size = kernel_size
        N = kernel_size ** 3

        # jy
        se_channels = in_channels // se_ratio
        self.conv_se = nn.Conv3d(in_channels, se_channels, kernel_size=1, bias=bias)

        self.stride = stride
        self.padding = padding
        self.zero_padding = nn.ConstantPad3d(padding, 0)
        # self.conv_kernel = nn.Conv3d(in_channels * N, out_channels, kernel_size=1, bias=bias)
        # self.offset_conv_kernel = nn.Conv3d(in_channels, N * 3, kernel_size=kernel_size, padding=padding, bias=bias)
        self.conv_kernel = nn.Conv3d(se_channels * N, out_channels, kernel_size=1, bias=bias)
        self.offset_conv_kernel = nn.Conv3d(se_channels, N * 3, kernel_size=kernel_size, padding=padding, bias=bias)
        self.att = SpacialAttention3D()  # add by jy

        self.mode = "deformable"

    def deformable_mode(self, on=True):  #
        if on:
            self.mode = "deformable"
        else:
            self.mode = "regular"

    def forward(self, x):
        # jy
        x = self.conv_se(x)

        if self.mode == "deformable":
            offset = self.offset_conv_kernel(x)
            offset = offset * self.att(offset)
        else:
            b, c, h, w, d = x.size()
            offset = torch.zeros(b, 3 * self.kernel_size ** 3, h, w, d).to(x)

        dtype = offset.data.type()
        ks = self.kernel_size
        N = offset.size(1) // 3

        if self.padding:
            x = self.zero_padding(x)

        # (b, 3N, h, w, d)
        p = self._get_p(offset, dtype)
        p = p[:, :, ::self.stride, ::self.stride, ::self.stride]

        # (b, h, w, d, 3N), N == ks ** 3, 3N - 3 coords for each point on the activation map
        p = p.contiguous().permute(0, 2, 3, 4, 1)  # 5D array

        q_sss = Variable(p.data, requires_grad=False).floor()  # point with all smaller coords
        #         q_sss = p.data.floor() - same? / torch.Tensor(p.data).floor()
        q_lll = q_sss + 1  # all larger coords

        # 8 neighbor points with integer coords
        q_sss = torch.cat([
            torch.clamp(q_sss[..., :N], 0, x.size(2) - 1),  # h_coord
            torch.clamp(q_sss[..., N:2 * N], 0, x.size(3) - 1),  # w_coord
            torch.clamp(q_sss[..., 2 * N:], 0, x.size(4) - 1)  # d_coord
        ], dim=-1).long()
        q_lll = torch.cat([
            torch.clamp(q_lll[..., :N], 0, x.size(2) - 1),  # h_coord
            torch.clamp(q_lll[..., N:2 * N], 0, x.size(3) - 1),  # w_coord
            torch.clamp(q_lll[..., 2 * N:], 0, x.size(4) - 1)  # d_coord
        ], dim=-1).long()
        q_ssl = torch.cat([q_sss[..., :N], q_sss[..., N:2 * N], q_lll[..., 2 * N:]], -1)
        q_sls = torch.cat([q_sss[..., :N], q_lll[..., N:2 * N], q_sss[..., 2 * N:]], -1)
        q_sll = torch.cat([q_sss[..., :N], q_lll[..., N:2 * N], q_lll[..., 2 * N:]], -1)
        q_lss = torch.cat([q_lll[..., :N], q_sss[..., N:2 * N], q_sss[..., 2 * N:]], -1)
        q_lsl = torch.cat([q_lll[..., :N], q_sss[..., N:2 * N], q_lll[..., 2 * N:]], -1)
        q_lls = torch.cat([q_lll[..., :N], q_lll[..., N:2 * N], q_sss[..., 2 * N:]], -1)

        # (b, h, w, d, N)
        mask = torch.cat([
            p[..., :N].lt(self.padding) + p[..., :N].gt(x.size(2) - 1 - self.padding),
            p[..., N:2 * N].lt(self.padding) + p[..., N:2 * N].gt(x.size(3) - 1 - self.padding),
            p[..., 2 * N:].lt(self.padding) + p[..., 2 * N:].gt(x.size(4) - 1 - self.padding),
        ], dim=-1).type_as(p)
        mask = mask.detach()
        floor_p = p - (p - torch.floor(p))  # все еще непонятно, что тут происходит за wtf
        p = p * (1 - mask) + floor_p * mask

        p = torch.cat([
            torch.clamp(p[..., :N], 0, x.size(2) - 1),
            torch.clamp(p[..., N:2 * N], 0, x.size(3) - 1),
            torch.clamp(p[..., 2 * N:], 0, x.size(4) - 1),
        ], dim=-1)

        # trilinear kernel (b, h, w, d, N)
        g_sss = (1 + (q_sss[..., :N].type_as(p) - p[..., :N])) * (
                    1 + (q_sss[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 + (q_sss[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_lll = (1 - (q_lll[..., :N].type_as(p) - p[..., :N])) * (
                    1 - (q_lll[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 - (q_lll[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_ssl = (1 + (q_ssl[..., :N].type_as(p) - p[..., :N])) * (
                    1 + (q_ssl[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 - (q_ssl[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_sls = (1 + (q_sls[..., :N].type_as(p) - p[..., :N])) * (
                    1 - (q_sls[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 + (q_sls[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_sll = (1 + (q_sll[..., :N].type_as(p) - p[..., :N])) * (
                    1 - (q_sll[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 - (q_sll[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_lss = (1 - (q_lss[..., :N].type_as(p) - p[..., :N])) * (
                    1 + (q_lss[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 + (q_lss[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_lsl = (1 - (q_lsl[..., :N].type_as(p) - p[..., :N])) * (
                    1 + (q_lsl[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 - (q_lsl[..., 2 * N:].type_as(p) - p[..., 2 * N:]))
        g_lls = (1 - (q_lls[..., :N].type_as(p) - p[..., :N])) * (
                    1 - (q_lls[..., N:2 * N].type_as(p) - p[..., N:2 * N])) * (
                            1 + (q_lls[..., 2 * N:].type_as(p) - p[..., 2 * N:]))

        # get values in all 8 neighbor points
        # (b, c, h, w, d, N) - 6D-array
        x_q_sss = self._get_x_q(x, q_sss, N)
        x_q_lll = self._get_x_q(x, q_lll, N)
        x_q_ssl = self._get_x_q(x, q_ssl, N)
        x_q_sls = self._get_x_q(x, q_sls, N)
        x_q_sll = self._get_x_q(x, q_sll, N)
        x_q_lss = self._get_x_q(x, q_lss, N)
        x_q_lsl = self._get_x_q(x, q_lsl, N)
        x_q_lls = self._get_x_q(x, q_lls, N)

        # (b, c, h, w, d, N)
        x_offset = g_sss.unsqueeze(dim=1) * x_q_sss + \
                   g_lll.unsqueeze(dim=1) * x_q_lll + \
                   g_ssl.unsqueeze(dim=1) * x_q_ssl + \
                   g_sls.unsqueeze(dim=1) * x_q_sls + \
                   g_sll.unsqueeze(dim=1) * x_q_sll + \
                   g_lss.unsqueeze(dim=1) * x_q_lss + \
                   g_lsl.unsqueeze(dim=1) * x_q_lsl + \
                   g_lls.unsqueeze(dim=1) * x_q_lls

        x_offset = self._reshape_x_offset(x_offset, ks)
        out = self.conv_kernel(x_offset)

        return out

    def _get_p_n(self, N, dtype):
        p_n_x, p_n_y, p_n_z = np.meshgrid(
            range(-(self.kernel_size - 1) // 2, (self.kernel_size - 1) // 2 + 1),
            range(-(self.kernel_size - 1) // 2, (self.kernel_size - 1) // 2 + 1),
            range(-(self.kernel_size - 1) // 2, (self.kernel_size - 1) // 2 + 1),
            indexing='ij')

        # (3N, 1) - 3 coords for each of N offsets
        # (x1, ... xN, y1, ... yN, z1, ... zN)
        p_n = np.concatenate((p_n_x.flatten(), p_n_y.flatten(), p_n_z.flatten()))
        p_n = np.reshape(p_n, (1, 3 * N, 1, 1, 1))
        p_n = torch.from_numpy(p_n).type(dtype)

        return p_n

    @staticmethod
    def _get_p_0(h, w, d, N, dtype):
        p_0_x, p_0_y, p_0_z = np.meshgrid(range(1, h + 1), range(1, w + 1), range(1, d + 1), indexing='ij')
        p_0_x = p_0_x.flatten().reshape(1, 1, h, w, d).repeat(N, axis=1)
        p_0_y = p_0_y.flatten().reshape(1, 1, h, w, d).repeat(N, axis=1)
        p_0_z = p_0_z.flatten().reshape(1, 1, h, w, d).repeat(N, axis=1)
        p_0 = np.concatenate((p_0_x, p_0_y, p_0_z), axis=1)
        p_0 = torch.from_numpy(p_0).type(dtype)

        return p_0

    def _get_p(self, offset, dtype):
        N, h, w, d = offset.size(1) // 3, offset.size(2), offset.size(3), offset.size(4)

        # (1, 3N, 1, 1, 1)
        p_n = self._get_p_n(N, dtype).to(offset)
        # (1, 3N, h, w, d)
        p_0 = self._get_p_0(h, w, d, N, dtype).to(offset)
        p = p_0 + p_n + offset

        return p

    def _get_x_q(self, x, q, N):
        b, h, w, d, _ = q.size()

        #           (0, 1, 2, 3, 4)
        # x.size == (b, c, h, w, d)
        padded_w = x.size(3)
        padded_d = x.size(4)
        c = x.size(1)
        # (b, c, h*w*d)
        x = x.contiguous().view(b, c, -1)

        # (b, h, w, d, N)
        # offset_x * w * d + offset_y * d + offset_z
        index = q[..., :N] * padded_w * padded_d + q[..., N:2 * N] * padded_d + q[..., 2 * N:]
        # (b, c, h*w*d*N)
        index = index.contiguous().unsqueeze(dim=1).expand(-1, c, -1, -1, -1, -1).contiguous().view(b, c, -1)

        x_offset = x.gather(dim=-1, index=index).contiguous().view(b, c, h, w, d, N)

        return x_offset

    @staticmethod
    def _reshape_x_offset(x_offset, ks):
        b, c, h, w, d, N = x_offset.size()
        x_offset = x_offset.permute(0, 1, 5, 2, 3, 4)
        x_offset = x_offset.contiguous().view(b, c * N, h, w, d)

        return x_offset




