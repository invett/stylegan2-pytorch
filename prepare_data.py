import argparse
from io import BytesIO
import multiprocessing
from functools import partial

from PIL import Image
import lmdb
from tqdm import tqdm
from torchvision import datasets
from torchvision.transforms import functional as trans_fn
import numpy as np

from torch.utils import data
from torchvision import transforms, utils
from dataloaders.sequencedataloader import txt_dataloader_styleGAN
from collections import Counter
import torchvision.transforms as transforms


def data_sampler(dataset, shuffle, distributed):
    if distributed:
        return data.distributed.DistributedSampler(dataset, shuffle=shuffle)

    if shuffle:
        return data.RandomSampler(dataset)

    else:
        return data.SequentialSampler(dataset)


def resize_and_convert(img, size, resample, quality=100):
    img = trans_fn.resize(img, size, resample)
    img = trans_fn.center_crop(img, size)
    buffer = BytesIO()
    img.save(buffer, format="jpeg", quality=quality)
    val = buffer.getvalue()

    return val


def resize_multiple(
    img, sizes=(128, 256, 512, 1024), resample=Image.LANCZOS, quality=100
):
    imgs = []

    for size in sizes:
        imgs.append(resize_and_convert(img, size, resample, quality))

    return imgs


def resize_worker(img_file, sizes, resample):
    i, file = img_file
    img = Image.open(file)
    img = img.convert("RGB")
    out = resize_multiple(img, sizes=sizes, resample=resample)

    return i, out


def prepare(
    env, dataset, n_worker, sizes=(128, 256, 512, 1024), resample=Image.LANCZOS
):
    resize_fn = partial(resize_worker, sizes=sizes, resample=resample)

    files = sorted(dataset.imgs, key=lambda x: x[0])
    labels = {i: label for i, (file, label) in enumerate(files)}
    np.save(env.path()+"/labels", labels)
    files = [(i, file) for i, (file, label) in enumerate(files)]
    total = 0

    with multiprocessing.Pool(n_worker) as pool:
        for i, imgs in tqdm(pool.imap_unordered(resize_fn, files)):
            for size, img in zip(sizes, imgs):
                key = f"{size}-{str(i).zfill(5)}-{labels[i]}".encode("utf-8")

                with env.begin(write=True) as txn:
                    txn.put(key, img)

            total += 1

        with env.begin(write=True) as txn:
            txn.put("length".encode("utf-8"), str(total).encode("utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preprocess images for model training")
    parser.add_argument("--out", type=str, help="filename of the result lmdb dataset")
    parser.add_argument(
        "--size",
        type=str,
        default="128,256,512,1024",
        help="resolutions of images for the dataset",
    )
    parser.add_argument(
        "--n_worker",
        type=int,
        default=8,
        help="number of workers for preparing dataset",
    )
    parser.add_argument(
        "--resample",
        type=str,
        default="lanczos",
        help="resampling methods for resizing images",
    )
    parser.add_argument("--path", type=str, action='append', help="path(s) to the image dataset", required=True)

    # parser.add_argument("--image_type", type=str, default='original-stylegan2',
    #                     help="Choose between warping or rgb or the original dataloader",
    #                     choices=['rgb', 'warping', 'original-stylegan2'])

    parser.add_argument("--batch_size", type=int, default=1, help="size of the batches")

    parser.add_argument('--decimate', type=int, default=1, help='select decimation modality for stylegan dataloader')
    parser.add_argument('--decimateAlcala', type=int, default=30, help='decimate step for alcala datasets')
    parser.add_argument('--decimateKitti', type=int, default=10, help='decimate step for kitti datasets')

    args = parser.parse_args()

    resample_map = {"lanczos": Image.LANCZOS, "bilinear": Image.BILINEAR}
    resample = resample_map[args.resample]

    sizes = [int(s.strip()) for s in args.size.split(",")]

    print(f"Make dataset of image sizes:", ", ".join(str(s) for s in sizes))

    # for GANS, normalize with these values https://pytorch.org/tutorials/beginner/dcgan_faces_tutorial.html
    # UPDATE: DISABLING THIS AS NOT PROVIDED WITH ORIGINAL datasets.ImageFolder(args.path)
    # rgb_image_test_transforms = transforms.Compose([transforms.Resize((224, 224)), transforms.ToTensor(),
    #                                                 transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])

    # warping   train_path = '/home/ballardini/DualBiSeNet/alcala-26.01.2021_selected_warped/prefix_all.txt'
    # rgb       train_path = '/home/ballardini/DualBiSeNet/alcala-26.01.2021_selected/prefix_all.txt'
    train_path = args.path
    dataset_ = txt_dataloader_styleGAN(train_path, decimateStep=args.decimate, decimateAlcala=args.decimateAlcala,
                                       decimateKitti=args.decimateKitti)
    imgset = dataset_

    ###############################################################  to check if everything is good
    if 1:
        loader = data.DataLoader(
            dataset_,
            batch_size=1,
            sampler=data_sampler(dataset_, shuffle=True, distributed=0),
            drop_last=True,
        )
        #check labels
        lab = []
        for i in range(len(loader.dataset.imgs)):
            lab.append(loader.dataset.__getitem__(i)[1])
        a = dict(Counter(lab))
        print('Double check. Should correspond to the above.')
        print(a)
    ###############################################################  to check if everything is good

    # default old/stylegan2
    # imgset = datasets.ImageFolder(args.path)

    print('Dataset will be written in:', str(args.out))

    exit(1)

    with lmdb.open(args.out, map_size=1024 ** 4, readahead=False) as env:
        prepare(env, imgset, args.n_worker, sizes=sizes, resample=resample)
