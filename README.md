# nlptermproject
this project is for the term project of the course AIE6211 in Sogang University. 

## Korean CLIP
We finetune CLIP : Contrastive Language Image Pretraining with MS-COCO Korean Caption Dataset.
To enhance the language ability, we utilize RoBERTa model pretrained in Korean Language, rather than training from scratch.
We use ResNet-101, ViT, ViT-DINO for the vision model.

## training
```
python train.py --train_coco_img_dir $datadir\
--train_coco_annotation_file $annotation_dir\
--valid_coco_img_dir $val_img_dir\
--valid_coco_annotation_file $val_annotation_dir\
--pvm RN101
```

## evaluation
```
python zeroshot_eval.py --checkpoint_path $checkpoint_path --pvm $image_encoder --template_version v1 --data_dir CIFAR100
```