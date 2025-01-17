import argparse

import torch
from torchvision import utils
from tqdm import tqdm


def generate(args, g_ema, device, mean_latent):

    with torch.no_grad():
        g_ema.eval()
        for i in tqdm(range(args.pics)):
           
            sample_z = torch.randn(args.sample, args.latent, device=device)
            
            if args.conditional:
                if args.label == -1:
                    for label in range(0,7):
                        labels = torch.tensor(label).repeat(args.sample)
                        labels = torch.nn.functional.one_hot(labels, num_classes=args.num_classes).float().to(device)
                        sample, _ = g_ema(
                            [sample_z], labels, truncation=args.truncation, truncation_latent=mean_latent
                        )
                        utils.save_image(
                        sample,
                        f"generated_samples/conditional/{args.file_name}-{str(label)}_{str(i).zfill(6)}.png",
                        nrow=1,
                        normalize=True,
                        range=(-1, 1),
                        )
                else:
                    labels = torch.tensor(args.label).repeat(args.sample)
                    labels = torch.nn.functional.one_hot(labels, num_classes=args.num_classes).float().to(device)
                    sample, _ = g_ema(
                        [sample_z], labels, truncation=args.truncation, truncation_latent=mean_latent        
                    )
                    utils.save_image(
                    sample,
                    f"generated_samples/conditional/{args.file_name}-{str(args.label)}_{str(i).zfill(6)}.png",
                    nrow=1,
                    normalize=True,
                    range=(-1, 1),
                    )
            else:
                sample, _ = g_ema(
                    [sample_z], truncation=args.truncation, truncation_latent=mean_latent
                )

                utils.save_image(
                    sample,
                    f"generated_samples/{args.file_name}-{str(i).zfill(6)}.png",
                    nrow=1,
                    normalize=True,
                    range=(-1, 1),
                )


if __name__ == "__main__":
    device = "cuda"

    parser = argparse.ArgumentParser(description="Generate samples from the generator")

    parser.add_argument(
        "--size", type=int, default=256, help="output image size of the generator"
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=1,
        help="number of samples to be generated for each image",
    )
    parser.add_argument(
        "--pics", type=int, default=20, help="number of images to be generated"
    )
    parser.add_argument("--truncation", type=float, default=1, help="truncation ratio")
    parser.add_argument(
        "--truncation_mean",
        type=int,
        default=4096,
        help="number of vectors to calculate mean for the truncation",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        default="stylegan2-ffhq-config-f.pt",
        help="path to the model checkpoint",
    )
    parser.add_argument(
        "--channel_multiplier",
        type=int,
        default=2,
        help="channel multiplier of the generator. config-f = 2, else = 1",
    )
    parser.add_argument(
        "--file_name",
        type=str,
        default="stylegan2-001",
        help="file name for the generated images <name>-<num_image>.png",
    )
    parser.add_argument(
        '--arch', 
        type=str, 
        default='stylegan2', 
        help='model architectures (stylegan2 | swagan)')
    parser.add_argument(
        "--conditional", 
        action="store_true", 
        help="conditional generation",
    )
    parser.add_argument(
        "--num_classes",
        type=int,
        default=7,
    )
    parser.add_argument(
        "--label",
        type=int,
        default=-1,
        help='Class to generate. -1 if generate all labels.',
    )


    args = parser.parse_args()

    args.latent = 512
    args.n_mlp = 8
    

    if args.arch == 'stylegan2':
        if args.conditional:
            from model_conditional import Generator
        else:
            from model import Generator

    elif args.arch == 'swagan':
        from swagan import Generator
        
    if args.conditional:
        g_ema = Generator(
            args.size, args.latent, args.n_mlp, num_classes=args.num_classes, channel_multiplier=args.channel_multiplier
        ).to(device)
    else:
        g_ema = Generator(
            args.size, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier
        ).to(device)
        
    checkpoint = torch.load(args.ckpt)

    g_ema.load_state_dict(checkpoint["g_ema"])

    if args.truncation < 1:
        with torch.no_grad():
            mean_latent = g_ema.mean_latent(args.truncation_mean)
    else:
        mean_latent = None

    generate(args, g_ema, device, mean_latent)
