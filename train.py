import argparse
import math
import os
import random

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.distributed as dist
from torch import nn, autograd, optim
from torch.nn import functional as F
from torch.utils import data
from torchvision import transforms, utils
from torchvision.utils import make_grid
from torchvision import models
from tqdm import tqdm
import time

from miscellaneous.utils import send_telegram_picture
from miscellaneous.utils import get_distances_embb, get_distances_embb_torch

from sklearn.preprocessing import RobustScaler, StandardScaler

try:
    import wandb

except ImportError:
    wandb = None

from sequencedataloader import txt_dataloader_styleGAN

from distributed import (get_rank, synchronize, reduce_loss_dict, reduce_sum, get_world_size, )
from op import conv2d_gradfix
from non_leaking import augment, AdaptiveAugment


class VGG(torch.nn.Module):

    def __init__(self, pretrained=True, embeddings=False, num_classes=None, version='vgg11', logits=False):
        super().__init__()
        if version == 'vgg11':
            model = models.vgg11_bn(pretrained=pretrained)
        if version == 'vgg13':
            model = models.vgg13_bn(pretrained=pretrained)
        if version == 'vgg16':
            model = models.vgg16_bn(pretrained=pretrained)
        if version == 'vgg19':
            model = models.vgg19_bn(pretrained=pretrained)

        self.embeddings = embeddings
        self.logits = logits

        self.features = model.features
        self.avgpool = model.avgpool

        if embeddings:
            self.classifier = model.classifier
            self.classifier[6] = torch.nn.Linear(4096, 512)
        else:
            self.classifier = model.classifier
            self.classifier[6] = torch.nn.Linear(4096, num_classes)

        if self.logits:
            self.softmax = torch.nn.LogSoftmax()

    def forward(self, data):
        features = self.features(data)
        avg = self.avgpool(features)
        avg = torch.flatten(avg, start_dim=1)
        prediction = self.classifier(avg)

        if self.logits and not self.embeddings:
            prediction = self.softmax(prediction)

        return prediction


def data_sampler(dataset, shuffle, distributed):
    if distributed:
        print('running DistributedSampler!')
        return data.distributed.DistributedSampler(dataset, shuffle=shuffle)

    if shuffle:
        return data.RandomSampler(dataset)

    else:
        return data.SequentialSampler(dataset)


def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag


def accumulate(model1, model2, decay=0.999):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())

    for k in par1.keys():
        par1[k].data.mul_(decay).add_(par2[k].data, alpha=1 - decay)


def sample_data(loader):
    while True:
        for batch in loader:
            yield batch


def d_logistic_loss(real_pred, fake_pred, centroid_distances=None):
    real_loss = F.softplus(-real_pred)
    fake_loss = F.softplus(fake_pred)
    if centroid_distances is not None:
        fake_distance_loss = F.softplus(centroid_distances).mean()
        return real_loss.mean() + fake_loss.mean() + fake_distance_loss, fake_distance_loss
    else:
        return real_loss.mean() + fake_loss.mean()


def d_r1_loss(real_pred, real_img):
    with conv2d_gradfix.no_weight_gradients():
        grad_real, = autograd.grad(outputs=real_pred.sum(), inputs=real_img, create_graph=True)
    grad_penalty = grad_real.pow(2).reshape(grad_real.shape[0], -1).sum(1).mean()

    return grad_penalty


def g_nonsaturating_loss(fake_pred, centroid_distances=None):
    if centroid_distances is not None:
        loss = F.softplus(-fake_pred).mean()
        fake_distance_loss = F.softplus(centroid_distances).mean()
        return loss + fake_distance_loss
    else:
        return F.softplus(-fake_pred).mean()


def g_path_regularize(fake_img, latents, mean_path_length, decay=0.01):
    noise = torch.randn_like(fake_img) / math.sqrt(fake_img.shape[2] * fake_img.shape[3])
    grad, = autograd.grad(outputs=(fake_img * noise).sum(), inputs=latents, create_graph=True)
    path_lengths = torch.sqrt(grad.pow(2).sum(2).mean(1))

    path_mean = mean_path_length + decay * (path_lengths.mean() - mean_path_length)

    path_penalty = (path_lengths - path_mean).pow(2).mean()

    return path_penalty, path_mean.detach(), path_lengths


def make_noise(batch, latent_dim, n_noise, device):
    if n_noise == 1:
        return torch.randn(batch, latent_dim, device=device)

    noises = torch.randn(n_noise, batch, latent_dim, device=device).unbind(0)

    return noises


def mixing_noise(batch, latent_dim, prob, device):
    if prob > 0 and random.random() < prob:
        return make_noise(batch, latent_dim, 2, device)

    else:
        return [make_noise(batch, latent_dim, 1, device)]


def set_grad_none(model, targets):
    for n, p in model.named_parameters():
        if n in targets:
            p.grad = None


def train(args, loader, generator, discriminator, g_optim, d_optim, g_ema, device, intesection_classificator,
          centroid_distances=None):
    loader = sample_data(loader)

    ############################### sklearn parameters #################################################################

    # load data
    KITTI_data = np.loadtxt('../../GAN-distances-analysis/rgb.alcala26.kitti.kitti360/distances_kitti_road_train.val.test.npy.txt')
    KITTI_labels = np.loadtxt('../../GAN-distances-analysis/rgb.alcala26.kitti.kitti360/KITTI-ROAD-prefix_all_v002.txt', usecols=(1), delimiter=';')
    ALCALA_data = np.loadtxt('../../GAN-distances-analysis/rgb.alcala26.kitti.kitti360/distances_alcala26_train.val.test.npy.txt')
    ALCALA_labels = np.loadtxt('../../GAN-distances-analysis/rgb.alcala26.kitti.kitti360/ALCALA26-prefix_all_nonewfiles_v002.txt', usecols=(1), delimiter=';')

    # create clusters
    KITTI_c_0 = KITTI_data[np.argwhere(KITTI_labels == 0)].squeeze()[:, 0].reshape(-1, 1)
    KITTI_c_1 = KITTI_data[np.argwhere(KITTI_labels == 1)].squeeze()[:, 1].reshape(-1, 1)
    KITTI_c_2 = KITTI_data[np.argwhere(KITTI_labels == 2)].squeeze()[:, 2].reshape(-1, 1)
    KITTI_c_3 = KITTI_data[np.argwhere(KITTI_labels == 3)].squeeze()[:, 3].reshape(-1, 1)
    KITTI_c_4 = KITTI_data[np.argwhere(KITTI_labels == 4)].squeeze()[:, 4].reshape(-1, 1)
    KITTI_c_5 = KITTI_data[np.argwhere(KITTI_labels == 5)].squeeze()[:, 5].reshape(-1, 1)
    KITTI_c_6 = KITTI_data[np.argwhere(KITTI_labels == 6)].squeeze()[:, 6].reshape(-1, 1)

    ALCALA_c_0 = ALCALA_data[np.argwhere(ALCALA_labels == 0)].squeeze()[:, 0].reshape(-1, 1)
    ALCALA_c_1 = ALCALA_data[np.argwhere(ALCALA_labels == 1)].squeeze()[:, 1].reshape(-1, 1)
    ALCALA_c_2 = ALCALA_data[np.argwhere(ALCALA_labels == 2)].squeeze()[:, 2].reshape(-1, 1)
    ALCALA_c_3 = ALCALA_data[np.argwhere(ALCALA_labels == 3)].squeeze()[:, 3].reshape(-1, 1)
    ALCALA_c_4 = ALCALA_data[np.argwhere(ALCALA_labels == 4)].squeeze()[:, 4].reshape(-1, 1)
    ALCALA_c_5 = ALCALA_data[np.argwhere(ALCALA_labels == 5)].squeeze()[:, 5].reshape(-1, 1)
    ALCALA_c_6 = ALCALA_data[np.argwhere(ALCALA_labels == 6)].squeeze()[:, 6].reshape(-1, 1)

    # mix alcala and kitti, create standard-scaler
    standardscaler_c_0 = StandardScaler().fit(np.vstack([KITTI_c_0, ALCALA_c_0]))
    standardscaler_c_1 = StandardScaler().fit(np.vstack([KITTI_c_1, ALCALA_c_1]))
    standardscaler_c_2 = StandardScaler().fit(np.vstack([KITTI_c_2, ALCALA_c_2]))
    standardscaler_c_3 = StandardScaler().fit(np.vstack([KITTI_c_3, ALCALA_c_3]))
    standardscaler_c_4 = StandardScaler().fit(np.vstack([KITTI_c_4, ALCALA_c_4]))
    standardscaler_c_5 = StandardScaler().fit(np.vstack([KITTI_c_5, ALCALA_c_5]))
    standardscaler_c_6 = StandardScaler().fit(np.vstack([KITTI_c_6, ALCALA_c_6]))

    # mix alcala and kitti, create robust-scaler
    robustscaler_c_0 = RobustScaler().fit(np.vstack([KITTI_c_0, ALCALA_c_0]))
    robustscaler_c_1 = RobustScaler().fit(np.vstack([KITTI_c_1, ALCALA_c_1]))
    robustscaler_c_2 = RobustScaler().fit(np.vstack([KITTI_c_2, ALCALA_c_2]))
    robustscaler_c_3 = RobustScaler().fit(np.vstack([KITTI_c_3, ALCALA_c_3]))
    robustscaler_c_4 = RobustScaler().fit(np.vstack([KITTI_c_4, ALCALA_c_4]))
    robustscaler_c_5 = RobustScaler().fit(np.vstack([KITTI_c_5, ALCALA_c_5]))
    robustscaler_c_6 = RobustScaler().fit(np.vstack([KITTI_c_6, ALCALA_c_6]))

    standardscalers = [standardscaler_c_0, standardscaler_c_1, standardscaler_c_2, standardscaler_c_3,
                       standardscaler_c_4, standardscaler_c_5, standardscaler_c_6]
    robustscalers = [robustscaler_c_0, robustscaler_c_1, robustscaler_c_2, robustscaler_c_3, robustscaler_c_4,
                     robustscaler_c_5, robustscaler_c_6]

    ############################### sklearn parameters #################################################################


    pbar = range(args.iter)

    if get_rank() == 0:
        pbar = tqdm(pbar, initial=args.start_iter, dynamic_ncols=True, smoothing=0.01)

    mean_path_length = 0

    d_loss_val = 0
    r1_loss = torch.tensor(0.0, device=device)
    g_loss_val = 0
    path_loss = torch.tensor(0.0, device=device)
    path_lengths = torch.tensor(0.0, device=device)
    mean_path_length_avg = 0
    loss_dict = {}

    if args.distributed:
        g_module = generator.module
        d_module = discriminator.module

    else:
        g_module = generator
        d_module = discriminator

    accum = 0.5 ** (32 / (10 * 1000))
    ada_aug_p = args.augment_p if args.augment_p > 0 else 0.0
    r_t_stat = 0

    if args.augment and args.augment_p == 0:
        ada_augment = AdaptiveAugment(args.ada_target, args.ada_length, 8, device)

    sample_z = torch.randn(args.n_sample, args.latent, device=device)

    for idx in pbar:
        i = idx + args.start_iter

        if i > args.iter:
            print("Done!")

            break

        real_img = next(loader)
        real_img = real_img.to(device)

        requires_grad(generator, False)
        requires_grad(discriminator, True)
        #requires_grad(intesection_classificator, False)
        #intesection_classificator.eval()

        noise = mixing_noise(args.batch, args.latent, args.mixing, device)
        fake_img, _ = generator(noise)

        # ACA PONER LO DE LA RED DE CLASIFICACION CRUCES, para el **DISCRIMINATOR**
        scaled_data = None
        scaled_data_reals = 0
        if centroid_distances is not None:
            batch_embeddings = intesection_classificator(fake_img)
            batch_distances_torch = get_distances_embb_torch(batch_embeddings, centroids)
            centroid_distances_torch, indices = torch.min(batch_distances_torch, 1)
            
            batch_embeddings_reals = intesection_classificator(real_img)
            batch_distances_torch_reals = get_distances_embb_torch(batch_embeddings_reals, centroids)
            centroid_distances_torch_reals, indices_reals = torch.min(batch_distances_torch_reals, 1)

            scaled_data = []
            scaled_data_reals = []
            for val, index, val_reals, index_reals in zip(centroid_distances_torch, indices, centroid_distances_torch_reals, indices_reals):
                if index == torch.tensor([0]).cuda():
                    scaled_data.append(robustscalers[0].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[0].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([1]).cuda():
                    scaled_data.append(robustscalers[1].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[1].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([2]).cuda():
                    scaled_data.append(robustscalers[2].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[2].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([3]).cuda():
                    scaled_data.append(robustscalers[3].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[3].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([4]).cuda():
                    scaled_data.append(robustscalers[4].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[4].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([5]).cuda():
                    scaled_data.append(robustscalers[5].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[5].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([6]).cuda():
                    scaled_data.append(robustscalers[6].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                    scaled_data_reals.append(robustscalers[6].transform(np.array(val_reals.item()).reshape(-1, 1))[0][0])

            # convert to tensor + gpu and send this value instead of centroid_distances_torch
            scaled_data = torch.tensor(scaled_data).to(device)
            scaled_data_reals = torch.tensor(scaled_data_reals).to(device)

        if args.augment:
            real_img_aug, _ = augment(real_img, ada_aug_p)
            fake_img, _ = augment(fake_img, ada_aug_p)

        else:
            real_img_aug = real_img

        fake_pred = discriminator(fake_img)
        real_pred = discriminator(real_img_aug)

        # we have to check what we want to log.. some tricks to avoid bugs
        if centroid_distances is not None:
            d_loss, distance_loss = d_logistic_loss(real_pred, fake_pred, scaled_data)
            distance_loss_reals = F.softplus(scaled_data_reals).mean()
        else:
            d_loss = d_logistic_loss(real_pred, fake_pred)
            # if i don't use "softplus" and i put the tensor, then error..
            # TODO: maybe would be nice to log even with no vgg!?
            distance_loss = F.softplus(torch.tensor([-100.]).to(device)).mean()
            distance_loss_reals = F.softplus(torch.tensor([-100.]).to(device)).mean()

        loss_dict["d"] = d_loss
        loss_dict["distance_loss"] = distance_loss
        loss_dict["distance_loss_reals"] = distance_loss_reals
        loss_dict["real_score"] = real_pred.mean()
        loss_dict["fake_score"] = fake_pred.mean()

        discriminator.zero_grad()
        d_loss.backward()
        d_optim.step()

        if args.augment and args.augment_p == 0:
            ada_aug_p = ada_augment.tune(real_pred)
            r_t_stat = ada_augment.r_t_stat

        d_regularize = i % args.d_reg_every == 0

        if d_regularize:
            real_img.requires_grad = True

            if args.augment:
                real_img_aug, _ = augment(real_img, ada_aug_p)

            else:
                real_img_aug = real_img

            real_pred = discriminator(real_img_aug)
            r1_loss = d_r1_loss(real_pred, real_img)

            discriminator.zero_grad()
            (args.r1 / 2 * r1_loss * args.d_reg_every + 0 * real_pred[0]).backward()

            d_optim.step()

        loss_dict["r1"] = r1_loss

        requires_grad(generator, True)
        requires_grad(discriminator, False)
        #requires_grad(intesection_classificator, False)
        #intesection_classificator.eval()


        noise = mixing_noise(args.batch, args.latent, args.mixing, device)
        fake_img, _ = generator(noise)

        # ACA PONER LO DE LA RED DE CLASIFICACION CRUCES, para el **GENERATOR**
        if centroid_distances is not None:
            batch_embeddings = intesection_classificator(fake_img)
            batch_distances_torch = get_distances_embb_torch(batch_embeddings, centroids)
            centroid_distances_torch, indices = torch.min(batch_distances_torch, 1)

            scaled_data = []
            for val, index in zip(centroid_distances_torch, indices):
                if index == torch.tensor([0]).cuda():
                    scaled_data.append(robustscalers[0].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([1]).cuda():
                    scaled_data.append(robustscalers[1].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([2]).cuda():
                    scaled_data.append(robustscalers[2].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([3]).cuda():
                    scaled_data.append(robustscalers[3].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([4]).cuda():
                    scaled_data.append(robustscalers[4].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([5]).cuda():
                    scaled_data.append(robustscalers[5].transform(np.array(val.item()).reshape(-1, 1))[0][0])
                if index == torch.tensor([6]).cuda():
                    scaled_data.append(robustscalers[6].transform(np.array(val.item()).reshape(-1, 1))[0][0])

            # convert to tensor + gpu and send this value instead of centroid_distances_torch
            scaled_data = torch.tensor(scaled_data).to(device)

            # this part of the code saves the distances, just to know if we're getting best results.

            # tosave = batch_embeddings.detach().cpu().numpy()
            # fp = '/home/ballardini/history' + str(int(time.time())) + '_' + str(os.getpid()) + '_' + '.npz'
            # if 'fp_old' in locals():
            #     f = np.load(fp_old, allow_pickle=True)
            #     os.system('rm ' + fp_old)
            #     f = f['embeddings']
            # else:
            #     f = np.empty((0, 512), dtype=tosave.dtype)
            #     fp_old = fp
            # f = np.vstack((f, tosave))
            # np.savez_compressed(fp, embeddings=f)
            # centroid_distances_torch = None
            # fp_old = fp

        if args.augment:
            fake_img, _ = augment(fake_img, ada_aug_p)

        fake_pred = discriminator(fake_img)
        g_loss = g_nonsaturating_loss(fake_pred, scaled_data) # esta es "unica", mixed.

        loss_dict["g"] = g_loss

        generator.zero_grad()
        g_loss.backward()
        g_optim.step()

        g_regularize = i % args.g_reg_every == 0

        if g_regularize:
            path_batch_size = max(1, args.batch // args.path_batch_shrink)
            noise = mixing_noise(path_batch_size, args.latent, args.mixing, device)
            fake_img, latents = generator(noise, return_latents=True)

            path_loss, mean_path_length, path_lengths = g_path_regularize(fake_img, latents, mean_path_length)

            generator.zero_grad()
            weighted_path_loss = args.path_regularize * args.g_reg_every * path_loss

            if args.path_batch_shrink:
                weighted_path_loss += 0 * fake_img[0, 0, 0, 0]

            weighted_path_loss.backward()

            g_optim.step()

            mean_path_length_avg = (reduce_sum(mean_path_length).item() / get_world_size())

        loss_dict["path"] = path_loss
        loss_dict["path_length"] = path_lengths.mean()

        accumulate(g_ema, g_module, accum)

        loss_reduced = reduce_loss_dict(loss_dict)

        d_loss_val = loss_reduced["d"].mean().item()
        g_loss_val = loss_reduced["g"].mean().item()
        distance_loss_val = loss_reduced["distance_loss"].mean().item()
        distance_loss_val_reals = loss_reduced["distance_loss_reals"].mean().item()
        r1_val = loss_reduced["r1"].mean().item()
        path_loss_val = loss_reduced["path"].mean().item()
        real_score_val = loss_reduced["real_score"].mean().item()
        fake_score_val = loss_reduced["fake_score"].mean().item()
        path_length_val = loss_reduced["path_length"].mean().item()

        if get_rank() == 0:
            pbar.set_description((f"d: {d_loss_val:.4f}; g: {g_loss_val:.4f}; r1: {r1_val:.4f}; "
                                  f"path: {path_loss_val:.4f}; mean path: {mean_path_length_avg:.4f}; "
                                  f"augment: {ada_aug_p:.4f}"))

            if wandb and args.wandb:
                wandb.log({"Generator": g_loss_val, "Discriminator": d_loss_val, "Augment": ada_aug_p, "Rt": r_t_stat,
                           "R1": r1_val, "Path Length Regularization": path_loss_val, "distance_loss": distance_loss_val,
                           "Mean Path Length": mean_path_length, "Real Score": real_score_val, "distance_loss_reals": distance_loss_val_reals,
                           "Fake Score": fake_score_val, "Path Length": path_length_val, })

            if i % 100 == 0:
                with torch.no_grad():
                    g_ema.eval()
                    sample, _ = g_ema([sample_z])
                    utils.save_image(sample, f"sample/{str(i).zfill(6)}.png", nrow=int(args.n_sample ** 0.5),
                                     normalize=True, range=(-1, 1), )
                    grid = make_grid(sample, nrow=int(args.n_sample ** 0.5), normalize=True, range=(-1, 1))
                    # Add 0.5 after unnormalizing to [0, 255] to round to nearest integer
                    ndarr = grid.mul(255).add_(0.5).clamp_(0, 255).permute(1, 2, 0).to('cpu',
                                                                                       torch.uint8).numpy().astype(
                        np.float32)
                    im = ndarr
                    label = 'GAN - GRID\ncurrent iter: ' + str(i)
                    a = plt.figure(dpi=600)
                    plt.imshow(ndarr.astype('uint8'))
                    send_telegram_picture(a, label)
                    plt.close('all')
                    if wandb and args.wandb:
                        wandb.log({"current grid": wandb.Image(im, caption=f"Iter:{str(i).zfill(6)}")})

            if i % 5000 == 0:
                if wandb.run is not None:
                    torch.save({"g": g_module.state_dict(), "d": d_module.state_dict(), "g_ema": g_ema.state_dict(),
                                "g_optim": g_optim.state_dict(), "d_optim": d_optim.state_dict(), "args": args,
                                "ada_aug_p": ada_aug_p, }, f"checkpoint/{wandb.run.name}_{str(i).zfill(6)}.pt", )
                else:
                    torch.save({"g": g_module.state_dict(), "d": d_module.state_dict(), "g_ema": g_ema.state_dict(),
                                "g_optim": g_optim.state_dict(), "d_optim": d_optim.state_dict(), "args": args,
                                "ada_aug_p": ada_aug_p, }, f"checkpoint/{str(i).zfill(6)}.pt", )


if __name__ == "__main__":

    print('main')

    device = "cuda"

    parser = argparse.ArgumentParser(description="StyleGAN2 trainer")

    parser.add_argument("--centroids", type=str, help="centroids for extra loss term", required=False)
    parser.add_argument('--load_path', type=str, help='Insert path to the testing pth (for network testing)')

    # parser.add_argument("--path", type=str, help="path to the lmdb dataset")
    parser.add_argument("--path", type=str, action='append', help="path(s) to the image dataset", required=True)

    parser.add_argument('--arch', type=str, default='stylegan2', help='model architectures (stylegan2 | swagan)')
    parser.add_argument("--iter", type=int, default=800000, help="total training iterations")
    parser.add_argument("--batch", type=int, default=16, help="batch sizes for each gpus")
    parser.add_argument("--n_sample", type=int, default=64, help="number of the samples generated during training", )
    parser.add_argument("--size", type=int, default=256, help="image sizes for the model")
    parser.add_argument("--r1", type=float, default=10, help="weight of the r1 regularization")
    parser.add_argument("--path_regularize", type=float, default=2, help="weight of the path length regularization", )
    parser.add_argument("--path_batch_shrink", type=int, default=2,
                        help="batch size reducing factor for the path length regularization (reduce memory consumption)", )
    parser.add_argument("--d_reg_every", type=int, default=16, help="interval of the applying r1 regularization", )
    parser.add_argument("--g_reg_every", type=int, default=4,
                        help="interval of the applying path length regularization", )
    parser.add_argument("--mixing", type=float, default=0.9, help="probability of latent code mixing")
    parser.add_argument("--ckpt", type=str, default=None, help="path to the checkpoints to resume training", )
    parser.add_argument("--lr", type=float, default=0.002, help="learning rate")
    parser.add_argument("--channel_multiplier", type=int, default=2,
                        help="channel multiplier factor for the model. config-f = 2, else = 1", )
    parser.add_argument("--wandb", action="store_true", help="use weights and biases logging")
    parser.add_argument("--local_rank", type=int, default=0, help="local rank for distributed training")
    parser.add_argument("--augment", action="store_true", help="apply non leaking augmentation")
    parser.add_argument("--augment_p", type=float, default=0,
                        help="probability of applying augmentation. 0 = use adaptive augmentation", )
    parser.add_argument("--ada_target", type=float, default=0.6,
                        help="target augmentation probability for adaptive augmentation", )
    parser.add_argument("--ada_length", type=int, default=500 * 1000,
                        help="target duraing to reach augmentation probability for adaptive augmentation", )
    parser.add_argument("--ada_every", type=int, default=256,
                        help="probability update interval of the adaptive augmentation", )

    parser.add_argument('--decimate', type=int, default=1, help='select decimation modality for stylegan dataloader')
    parser.add_argument('--decimateAlcala', type=int, default=30, help='decimate step for alcala datasets')
    parser.add_argument('--decimateKitti', type=int, default=10, help='decimate step for kitti datasets')

    args = parser.parse_args()

    n_gpu = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    args.distributed = n_gpu > 1

    print('------------------------------------------')
    print('args.distributed= ', str(args.distributed))
    print('GPUS=             ', str(n_gpu))
    print('------------------------------------------')

    if args.distributed:
        print("inside if; args.local_rank= ", str(args.local_rank))
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
        synchronize()

    print('PASSED')
    print('------------------------------------------')

    args.latent = 512
    args.n_mlp = 8

    args.start_iter = 0

    if args.arch == 'stylegan2':
        from model import Generator, Discriminator

    elif args.arch == 'swagan':
        from swagan import Generator, Discriminator

    generator = Generator(args.size, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier).to(device)
    discriminator = Discriminator(args.size, channel_multiplier=args.channel_multiplier).to(device)
    g_ema = Generator(args.size, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier).to(device)
    g_ema.eval()
    accumulate(g_ema, generator, 0)

    g_reg_ratio = args.g_reg_every / (args.g_reg_every + 1)
    d_reg_ratio = args.d_reg_every / (args.d_reg_every + 1)

    g_optim = optim.Adam(generator.parameters(), lr=args.lr * g_reg_ratio,
                         betas=(0 ** g_reg_ratio, 0.99 ** g_reg_ratio), )
    d_optim = optim.Adam(discriminator.parameters(), lr=args.lr * d_reg_ratio,
                         betas=(0 ** d_reg_ratio, 0.99 ** d_reg_ratio), )

    if args.ckpt is not None:
        print("load model:", args.ckpt)

        ckpt = torch.load(args.ckpt, map_location=lambda storage, loc: storage)

        try:
            ckpt_name = os.path.basename(args.ckpt)
            args.start_iter = int(os.path.splitext(ckpt_name)[0])

        except ValueError:
            pass

        generator.load_state_dict(ckpt["g"])
        discriminator.load_state_dict(ckpt["d"])
        g_ema.load_state_dict(ckpt["g_ema"])

        g_optim.load_state_dict(ckpt["g_optim"])
        d_optim.load_state_dict(ckpt["d_optim"])

    if args.distributed:
        generator = nn.parallel.DistributedDataParallel(generator, device_ids=[args.local_rank],
                                                        output_device=args.local_rank, broadcast_buffers=False, )

        discriminator = nn.parallel.DistributedDataParallel(discriminator, device_ids=[args.local_rank],
                                                            output_device=args.local_rank, broadcast_buffers=False, )

    transform = transforms.Compose([transforms.Resize((256, 256)),
                                    # please, no: transforms.RandomHorizontalFlip(),
                                    transforms.ToTensor(),
                                    transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True), ])

    # codigo para crear la red de intersections y cargar centroides
    centroids = None
    intesection_classificator = None

    if args.centroids:

        # RED
        intesection_classificator = VGG(pretrained=False, embeddings=True, num_classes=7, version='vgg16', logits=False).to(device)
        intesection_classificator.eval()
        loadpath = args.load_path
        if os.path.isfile(loadpath):
            print("=> Loading checkpoint '{}' ... ".format(loadpath))
            checkpoint = torch.load(loadpath, map_location='cpu')
            intesection_classificator.load_state_dict(checkpoint['model_state_dict'])
            print("=> OK! Checkpoint loaded! '{}'".format(loadpath))
        else:
            print("=> no checkpoint found at '{}'".format(loadpath))

        # LOAD CENTROIDS
        ct_folder = '/media/14TBDISK/ballardini/trainedmodels/centroids/'
        ct_name = 'centroids.npy'
        if os.path.isfile(os.path.join(ct_folder, ct_name)):
            centroids = np.load(os.path.join(ct_folder, ct_name))
        else:

            gt_list = []
            embeddings = np.loadtxt('/media/14TBDISK/ballardini/trainedmodels/embeddings/embeddings.txt',
                                    delimiter='\t')
            labels = np.loadtxt('/media/14TBDISK/ballardini/trainedmodels/embeddings/labels.txt', delimiter='\t')
            for i in range(7):
                gt_list.append(np.mean(embeddings[labels == i], axis=0))
            centroids = np.array(gt_list)
            np.save(os.path.join(ct_folder, ct_name), centroids)

        centroids = torch.cuda.FloatTensor(centroids)

    # dataset = MultiResolutionDataset(args.path, transform, args.size)
    dataset = txt_dataloader_styleGAN(args.path, transform=transform, decimateStep=args.decimate,
                                      decimateAlcala=args.decimateAlcala, decimateKitti=args.decimateKitti,
                                      conditional=False)
    loader = data.DataLoader(dataset, batch_size=args.batch,
                             sampler=data_sampler(dataset, shuffle=True, distributed=args.distributed),
                             drop_last=True, )

    # check labels
    # lab = []
    # for i in range(len(loader.dataset.imgs)):<
    #     lab.append(loader.dataset.__getitem__(i)[1])
    # a = dict(Counter(lab))
    # print('Double check. Should correspond to the above.')
    # print(a)

    if get_rank() == 0 and wandb is not None and args.wandb:
        wandb.init(project="stylegan 2")

    train(args, loader, generator, discriminator, g_optim, d_optim, g_ema, device,
          intesection_classificator=intesection_classificator, centroid_distances=centroids)
