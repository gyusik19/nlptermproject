import torch
import torch.nn.functional as F
import numpy as np
import os
from omegaconf import OmegaConf

from data_loaders import KoCLIP_CUSTOM_dataset, get_dataloader

from model import KoCLIP
from utils.custom_schedulers import get_cosine_schedule_with_warmup, get_cosine_with_hard_restarts_schedule_with_warmup
from utils.util import set_seed, mkdir, load_config_file
from utils.logger import setup_logger

from torch.optim import AdamW

import argparse
import wandb
import datetime
from tqdm import tqdm

DATA_CONFIG_PATH = 'config_data.yaml'
TRAINER_CONFIG_PATH = 'config_train.yaml'

def train(config, train_dataset, valid_dataset, model):
    '''
    Trains the model.
    '''
    
    config.train_batch_size = config.per_gpu_train_batch_size * max(1, config.n_gpu)    
    train_dataloader = get_dataloader(config, train_dataset, is_train=True)
    valid_dataloader = get_dataloader(config, valid_dataset, is_train=False)

    # total training iterations
    t_total = len(train_dataloader) // config.gradient_accumulation_steps* config.num_train_epochs
    
    optimizer = AdamW(model.parameters(), lr=config.optimizer.params.lr, eps=config.optimizer.params.eps, weight_decay=config.optimizer.params.weight_decay)

    # Warmup iterations = 20% of total iterations
    num_warmup_steps = int(0.20 * t_total)
    scheduler = get_cosine_schedule_with_warmup(optimizer, num_warmup_steps= num_warmup_steps, num_training_steps= t_total)

    if config.n_gpu > 1:
        model = torch.nn.DataParallel(model)
    
    model = model.to(torch.device(config.device))

    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", config.num_train_epochs)
    logger.info("  Number of GPUs = %d", config.n_gpu)

    logger.info("  Batch size per GPU = %d", config.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, & accumulation) = %d", config.train_batch_size * config.gradient_accumulation_steps)
    logger.info("  Gradient Accumulation steps = %d", config.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)
    if scheduler:
        logger.info("  warmup steps = %d", num_warmup_steps)


    global_step, global_loss = 0,  0.0
    model.zero_grad()

    for epoch in range(int(config.num_train_epochs)):
        train_img_acc = 0.0
        model.train()
        for step, batch in enumerate(tqdm(train_dataloader)):
            input_images, input_texts = batch

            input_images = input_images.to(torch.device(config.device))

            input_texts = {b_key:b_item.squeeze().to(torch.device(config.device)) for b_key, b_item in input_texts.items()}

            image_features, text_features = model(input_images, input_texts)

            # normalized features
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)

            if config.n_gpu == 1:
                logit_scale = model.logit_scale.exp()
            elif config.n_gpu > 1:
                logit_scale = model.module.logit_scale.exp()

            logits_per_image = logit_scale * image_features @ text_features.t()
            logits_per_text = logit_scale * text_features @ image_features.t()
            labels = torch.arange(len(logits_per_image)).to(logits_per_image.device)

            train_img_acc += (logits_per_image.argmax()==labels).sum()
            image_loss = F.cross_entropy(logits_per_image, labels)
            text_loss  = F.cross_entropy(logits_per_text, labels)

            loss = (image_loss + text_loss) / 2

            if config.n_gpu > 1: 
                loss = loss.mean() # mean() to average on multi-gpu parallel training
            if config.gradient_accumulation_steps > 1:
                loss = loss / config.gradient_accumulation_steps
            loss.backward()
			
            global_loss += loss.item()
            if config.wandb:
                wandb.log({"Train Loss": loss.item(), "Train Img Loss": image_loss.mean().item(), "Train Txt Loss": text_loss.mean().item(), "lr": optimizer.param_groups[0]["lr"]})
            
            if (step + 1) % config.gradient_accumulation_steps == 0:
                global_step += 1
                optimizer.step() # PYTORCH 1.x : call optimizer.step() first then scheduler.step()
                
                # logit scaling set as max 100 as mentioned in CLIP paper # log(100) = 4.6052
                if config.n_gpu == 1:
                    model.logit_scale.data = torch.clamp(model.logit_scale.data, 0, 4.6052)
                elif config.n_gpu > 1:
                    model.module.logit_scale.data = torch.clamp(model.module.logit_scale.data, 0, 4.6052)

                if scheduler:
                    scheduler.step() 
                    
                model.zero_grad()

                if global_step % config.logging_steps == 0:
                    logger.info("Epoch: {}, global_step: {}, lr: {:.6f}, loss: {:.4f} ({:.4f}), train_img_acc: {:.4f}".format(epoch, global_step, optimizer.param_groups[0]["lr"], loss.item(), global_loss / global_step, train_img_acc))

                if (config.save_steps > 0 and global_step % config.save_steps == 0) or global_step == t_total: 
                    # saving checkpoint
                    save_checkpoint(config, epoch, global_step, model, optimizer) 
		
		### validation
        valid_loss, valid_img, valid_txt = 0.0, 0.0, 0.0
        with torch.no_grad():
            model.eval()
            val_img_acc = 0.0
            for final_step, batch in enumerate(tqdm(valid_dataloader)):
                input_images, input_texts = batch
                input_images = input_images.to(torch.device(config.device))

                input_texts = {b_key:b_item.squeeze().to(torch.device(config.device)) for b_key, b_item in input_texts.items()}

                image_features, text_features = model(input_images, input_texts)

                # normalized features
                image_features = image_features / image_features.norm(dim=-1, keepdim=True)
                text_features = text_features / text_features.norm(dim=-1, keepdim=True)

                if config.n_gpu == 1:
                    logit_scale = model.logit_scale.exp()
                elif config.n_gpu > 1:
                    logit_scale = model.module.logit_scale.exp()

                logits_per_image = logit_scale * image_features @ text_features.t()
                logits_per_text = logit_scale * text_features @ image_features.t()

                labels = torch.arange(len(logits_per_image)).to(logits_per_image.device)
                
                val_img_acc += (logits_per_image.argmax()==labels).sum()

                image_loss = F.cross_entropy(logits_per_image, labels)
                text_loss  = F.cross_entropy(logits_per_text, labels)

                loss = (image_loss + text_loss) / 2

                if config.n_gpu > 1: 
                    loss = loss.mean() # mean() to average on multi-gpu parallel training
                valid_loss += loss.item()
                valid_img += image_loss.item()
                valid_txt += text_loss.item()
            if config.wandb:
                wandb.log({"Valid Loss": valid_loss/(final_step+1), "Valid Img Loss": valid_img/(final_step+1), "Valid Txt Loss": valid_txt/(final_step+1)}) # , "Avg Loss": global_loss / global_step})
            
            logger.info("val loss: {:.4f}, val_img_acc: {:.4f}".format(valid_loss/(final_step+1), val_img_acc)
                    )
                    

    return global_step, global_loss / global_step


def save_checkpoint(config, epoch, global_step, model, optimizer):
    '''
    Checkpointing. Saves model and optimizer state_dict() and current epoch and global training steps.
    '''
    checkpoint_path = os.path.join(config.saved_checkpoints, f'checkpoint_{epoch}_{global_step}_{datetime.datetime.now()}.pt')
    save_num = 0
    while (save_num < 10):
        try:
            if config.n_gpu > 1:
                torch.save({'epoch' : epoch, 'global_step' : global_step, 'model_state_dict' : model.module.state_dict(), 'optimizer_state_dict': optimizer.state_dict()}, checkpoint_path)
            else:
                torch.save({'epoch' : epoch, 'global_step' : global_step, 'model_state_dict' : model.state_dict(), 'optimizer_state_dict': optimizer.state_dict()}, checkpoint_path)
            logger.info("Save checkpoint to {}".format(checkpoint_path))
            break
        except:
            save_num += 1
    if save_num == 10:
        logger.info("Failed to save checkpoint after 10 trails.")
    return

def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--train_coco_img_dir", default=None, type=str, required=False, help="path of directory containing COCO training images")
    parser.add_argument("--train_coco_annotation_file", default=None, type=str, required=False, help="path of COCO annotation file")
    parser.add_argument("--valid_coco_img_dir", default=None, type=str, required=False, help="path of directory containing COCO training images")
    parser.add_argument("--valid_coco_annotation_file", default=None, type=str, required=False, help="path of COCO annotation file")
    parser.add_argument("--pvm", default=None, type=str)
    args = parser.parse_args()
    
    train_config = load_config_file(TRAINER_CONFIG_PATH)
    data_config = load_config_file(DATA_CONFIG_PATH)
    

    config = OmegaConf.merge(train_config, data_config)
    if config.wandb:
        wandb.init(project="koclip", entity="gyusik19", config=config)
    # merging cli arguments, if data path given in cli args use those
    if args.train_coco_img_dir : 
        config.train_coco_img_dir = args.train_coco_img_dir
    if args.train_coco_annotation_file : 
        config.train_coco_annotation_file = args.train_coco_annotation_file
    if args.valid_coco_img_dir : 
        config.valid_coco_img_dir = args.valid_coco_img_dir
    if args.valid_coco_annotation_file : 
        config.valid_coco_annotation_file = args.valid_coco_annotation_file
    
    global logger
    # creating directories for saving checkpoints and logs
    mkdir(path=config.saved_checkpoints)
    mkdir(path=config.logs)

    logger = setup_logger("Ko-CLIP_TRAIN", config.logs, 0, filename = "training_logs.txt")

    config.device = "cuda" if torch.cuda.is_available() else "cpu"
    config.n_gpu = torch.cuda.device_count() # config.n_gpu 
    set_seed(seed=11, n_gpu=config.n_gpu)
    
    model_params = {'pvm':args.pvm, 'embed_dim':512}
    model = KoCLIP(**model_params)

    logger.info(f"Training/evaluation parameters {train_config}")

    # getting dataset for training
    train_dataset = KoCLIP_CUSTOM_dataset(config.train_coco_annotation_file, config.train_coco_img_dir)
    if config.vizwiz:
        vizwiz_train = KoCLIP_CUSTOM_dataset(config.train_vizwiz_annotation_file, config.train_vizwiz_img_dir, img_type='vizwiz')
        vizwiz_valid = KoCLIP_CUSTOM_dataset(config.valid_vizwiz_annotation_file, config.valid_vizwiz_img_dir, img_type='vizwiz')
        train_dataset = train_dataset.__add__(vizwiz_train)
        train_dataset = train_dataset.__add__(vizwiz_valid)
    
    valid_dataset = KoCLIP_CUSTOM_dataset(config.valid_coco_annotation_file, config.valid_coco_img_dir)

    # Now training
    global_step, avg_loss = train(config, train_dataset, valid_dataset, model)
    
    logger.info("Training done: total_step = %s, avg loss = %s", global_step, avg_loss)
    

if __name__ == "__main__":
    main()
