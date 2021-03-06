import torch
from torch.utils.data import DataLoader
from torch import no_grad
import os
from torch.cuda.amp import GradScaler, autocast
import time
from torch.utils.tensorboard import SummaryWriter
import warnings
import torch.nn.functional as F
import shutil
from torchvision.utils import save_image

from utils._DEPRECATED_GS_Dataloader import Make_Dataset
from utils.Dataloader_breastUS import ImageToImage2D,Image2D,JointTransform2D
from utils import loss_fn, Use_model
# from utils.Use_model import Use_model, use_scheduler
from utils.Use_model import *
from show_img import Save_image
from utils.Other_utils import *


import argparse
import pdb

def main(args):
    device = args.device
    save_freq = args.save_freq
    gamma = args.gamma
    args.use_autocast = bool(args.use_autocast)
    # 選擇使用的資料集：選擇有
    # [Gland Segmentation in Colon Histology Images Challenge]：用GS_Dataloader.py導入，
    # [breast Ultrasound lmage Dataset]：用Dataloader_breastUS.py導入
    if args.dataset == 'GS':
        train_dataset = Make_Dataset(args.train_dataset,img_size=(args.imgsize,args.imgsize))
        train_dataset = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    elif args.dataset == 'BreastUS':
        train_dataset = ImageToImage2D(args.train_dataset,img_size=(args.imgsize,args.imgsize))
        train_dataset = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    # ----- 確定使用model種類 -----
    model = use_model(args)

    # ----- 選擇optimizer種類，default: adam -----
    optimizer = use_opt(args, model)

    # ----- 學習率動態調整方法，default: CosineAnnealingLR -----
    scheduler = use_scheduler(args, optimizer)

    scaler = GradScaler(enabled=args.use_autocast)
    # ----- 初始化訓練結果資料夾(清空) -----
    init_training_result_folder()

    # ----- 紀錄訓練狀況 -----
    writer = SummaryWriter('./Model_Result/log')

    time_start = time.time()

    def train_epoch(test_epoch, safe_mode=1):
        '''
        :param
        test_epoch：需要訓練幾個epoch，代表最終訓練的epoch數量，屬於暫時型限制，最終訓練時test_epoch==arg.epoch
        save_freq：多少個epoch儲存一次checkpoint，預設為1
        '''
        epoch = args.epoch
        # best_loss = 100
        # best_model = ''

        def train_one_epoch(dataloader, model):
            '''
            training 1 epoch is packed in training epochs
            :param
                use_autocast: whether use automatic mixed precision training, default True
            '''
            model.to(device)
            model.train()
            train_accumulation_steps = args.train_accumulation_steps # iters per update
            for i, (image, mask) in enumerate(dataloader):
                image, mask = image.to(device), mask.to(device)  # b c h w
                with autocast(enabled=args.use_autocast):
                    output, _ = model(image)  # b,c,h,w

                    # loss計算
                    loss = choose_loss_fn(output, mask) / train_accumulation_steps
                    # if args.multilayer:
                    #     loss_feature = choose_loss_fn(feature, mask) / train_accumulation_steps
                    #     loss = gamma * loss_feature + (1-gamma) * loss
                # =================backward==================
                if args.use_autocast:
                    # 使用混合精度訓練
                    scaler.scale(loss).backward(retain_graph=True)
                    scaler.step(optimizer)
                    scaler.update()
                if args.use_autocast == False:
                    loss.backward()
                    if i % train_accumulation_steps == 0:
                        optimizer.step()
                        optimizer.zero_grad()
                # 僅供測試少量資料使用，確認程式可以運行
                if args.run_formal == 'no' and i == 8:
                    scheduler.step(loss)
                    return loss
                # return all losses, including weight_cross_entropy_loss,dice_coef_loss_loss,IoU_loss,FocalLoss_loss
                if i + 1 == len(dataloader):
                    scheduler.step(loss)
                    return loss

        # 測試val資料集，比較結果包含原影像、model輸出、GT
        def eval(val_dataset, model, epoch, binarization=False, scaling=False):
            model.eval()
            model.to('cpu')
            print('start eval!!!')
            save_path = r'./Model_Result/val_images'
            save_path = fr'{save_path}/epoch{epoch}'
            test_loss, f1, iou, iou_test = 0., 0., 0., 0.

            if not os.path.exists(save_path):
                os.makedirs(save_path)
            for i, (original_image, mask, original_size) in enumerate(val_dataset):
                x = original_image.to(torch.float32)
                with no_grad():
                    pred, _ = model(x) # b,c,h,w
                    # Use loss function
                    if scaling:
                        pred = loss_fn.sigmoid_scaling(pred) # sigmoid歸一化
                    if binarization:
                        pred = (pred > args.threshold).float() # th自行設定，設定為-1效果還不錯(不使用歸一化的狀況)，詳細看loss_fn單元測試
                    test_loss += choose_loss_fn(pred, mask)
                    f1 += loss_fn.classwise_f1(pred, mask, args.threshold)
                    iou += loss_fn.IoU(pred, mask, args.threshold)
                # print('pred.shape：',pred.shape)
                Save_image(original_image, pred, mask,
                           save_path=fr'{save_path}/num{i + 1}',
                           original_size=original_size,
                           channel=args.imgchan,
                           th=int(args.threshold * 255),
                           resize=args.savefig_resize
                           )

            val_loss = test_loss / len(val_dataset)
            f1 = f1 / len(val_dataset)
            iou = iou / len(val_dataset)
            iou_test = iou_test / len(val_dataset)
            print(f'epoch {epoch} validation.',
                  'avg_eval_loss：{:.4f}, '
                  'f1 score：{:.4f},'
                  ' mIoU score：{:.4f}'.format(test_loss,f1.item(),iou.item()),
                  sep='\t')
            return val_loss, f1, iou, iou_test

        if args.use_autocast:  # only appear when first sample of epoch
            print('=' * 10, 'use_autocast!', '=' * 10)
        else:
            print('=' * 10, 'NO use_autocast!', '=' * 10)
        for i, _ in enumerate(range(epoch)):
            # return 4 type of loss
            loss = train_one_epoch(train_dataset, model)
            writer.add_scalar(f'training {args.loss_fn} loss', scalar_value=loss, global_step=i)

            print(f'1 epoch loss = {loss}')

            if i % int(save_freq) == 0:
                model.eval()
                # 載入驗證資料集
                val_dataset = Image2D(args.val_dataset, img_size=(args.imgsize, args.imgsize))
                val_dataset = DataLoader(val_dataset)
                val_loss, f1, iou, iou_test = eval(val_dataset,model,i, binarization=False, scaling=False) # must set scaling and binarization
                writer.add_scalar('val_loss', scalar_value=val_loss, global_step=i)
                writer.add_scalar('f1 score', scalar_value=f1, global_step=i)
                writer.add_scalar('mIoU score', scalar_value=iou, global_step=i)
                writer.add_scalar('iou_myself', scalar_value=iou_test, global_step=i)
                if args.savemodel:
                    save_model_mode(model, safe_mode, save_freq, i)

            if i+1 == epoch:  # 測試用的epoch, test_epoch=1 代表訓練兩個epoch
                print('='*10,'last one eval','='*10)
                val_dataset = Image2D(args.val_dataset, img_size=(args.imgsize, args.imgsize))
                val_dataset = DataLoader(val_dataset)
                eval(val_dataset, model, i+1, binarization=False, scaling=False)
                print('='*10,'last one eval finish!!!!!','='*10)
                if args.savemodel:
                    save_model_mode(model, safe_mode, save_freq, i+1)
                break
    # 訓練(迭代)階段
    train_epoch(args.epoch, safe_mode=1)
    # 計算時間(以秒為單位)
    time_end = time.time()
    print('training Done! Use {:.2f} s'.format(time_end - time_start))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Transformer Test Version')

    # Training parameter setting
    parser.add_argument('--epoch', default=3, type=int, help='需要跑的輪數')
    parser.add_argument('-bs', '--batch_size', default=1, type=int)
    parser.add_argument('-is', '--imgsize', type=int, default=256, help='圖片大小')
    parser.add_argument('-ic', '--imgchan', type=int, default=2, help='model輸出影像通道數(grayscale)')
    parser.add_argument('-model', '--modelname', required=True, type=str)
    parser.add_argument('-ds_path', '--train_dataset', required=True, type=str, help='訓練資料集位置')
    parser.add_argument('-vd', '--val_dataset', type=str, help='驗證用資料集所在位置')



    # Model training setting
    parser.add_argument('--device', default='cuda', help='是否使用GPU訓練')
    parser.add_argument('-ds', '--dataset', choices=['GS','BreastUS'],default='GS',help='選擇使用的資料集，默認GS，可以選擇BreastUS')
    parser.add_argument('--use_autocast', type=int, help='是否使用混和精度訓練')
    parser.add_argument('--threshold', default=0.333, type=int, help='設定model output後二值化的threshold, 介於0-1之間')
    parser.add_argument('--train_accumulation_steps', default=4, type=int, help='多少iters更新一次權重(可減少顯存負擔)')
    parser.add_argument('--multilayer', default=True, type=bool, help='是否使用多multilayer model')
    parser.add_argument('--gamma', default=0.5, type=float, help='double loss的分配比例')

    # Optimizer Setting
    parser.add_argument('--lr', type=float, default=0.001, help='learning rate')
    parser.add_argument('--scheduler', type=str, default='ReduceLROnPlateau', help='使用的scheduler')
    parser.add_argument('-opt', '--optimizer', type=str, default='adam', help='使用的optimizer')

    # Loss function and Loss schedule
    parser.add_argument('-loss', '--loss_fn', default='wce',
                        choices=['wce', 'dice_coef_loss', 'IoU', 'FocalLoss', 'bce', 'lll', 'clsiou'])
    parser.add_argument('-wce', '--wce_beta', type=float, default=1e-04, help='wce_loss的wce_beta值，如果使用wce_loss時需要設定')

    # Save Setting
    parser.add_argument('-sf', '--save_freq', default=1,type=int, help='多少個epoch儲存一次checkpoint')
    parser.add_argument('--save_state_dict', type=bool, default=True, help='是否只儲存權重，默認為權重')
    parser.add_argument('--savemodel', type=bool, default=False, help='是否儲存模型')
    parser.add_argument('-r', '--run_formal', default='yes', type=str, choices=['yes', 'no'], help='是否是正式訓練(if not, train 20 iters for each epoch)')
    parser.add_argument('--direc', default='./Model_Result', type=str, help='directory to save')
    parser.add_argument('--savefig_resize', default=False, type=bool, help='savefig resize')

    args = parser.parse_args()


    # =============================額外增加功能放在這邊=============================
    def save_training_images(image):
        '''儲存訓練過程產生的影像，假設輸入影像大小為(1,c,h,w)'''
        b,c,h,w = image.shape
        assert c in [1,2,3], f'c is not in [1,2,3], c is {c}'
        if torch.is_tensor(image):
            image = image.squeeze(0)
        save_image(image, './Model_Result/test_files/test_image.png')
    def choose_loss_fn(output, target):
        # 選擇使用的loss function。
        # 經過測試可以使用的(在MedT環境中)：weight_cross_entropy, dice_coef_loss,IoU,FocalLoss
        # validation階段調用
        if args.loss_fn == 'wce':
            loss_fn_name = 'wce'
            loss = loss_fn.weight_cross_entropy(output, target, wce_beta=args.wce_beta)
        elif args.loss_fn == 'dice_coef_loss':
            loss_fn_name = 'dice_coef_loss'
            loss = loss_fn.dice_coef_loss(output, target)
        elif args.loss_fn == 'IoU':
            loss_fn_name = 'IoU'
            loss = loss_fn.IoU(output, target)
        elif args.loss_fn == 'FocalLoss':  # 這個criterion是用torch.nn.module建立，需要當作layer看待
            loss_fn_name = 'FocalLoss'
            loss = loss_fn.FocalLoss()(output, target)
        elif args.loss_fn == 'lll':
            loss_fn_name = 'lll'
            loss = loss_fn.LogNLLLoss()(output, target)
        elif args.loss_fn == 'clsiou':
            loss_fn_name = 'clsiou'
            loss = loss_fn.classwise_iou(output, target)
        # print('----- loss_fn_name: ', loss_fn_name, '-----')
        return loss

    def init_training_result_folder():
        '''初始化並創建資料夾'''
        path = './Model_Result'
        files = os.listdir(path)
        for file in files:
            cur_path = os.path.join(path,file)
            if os.path.isdir(cur_path):
                shutil.rmtree(cur_path, onerror=remove_readonly)
            else:
                os.remove(cur_path)
        if not os.path.exists('./Model_Result/log'):
            os.makedirs('./Model_Result/log')
        if not os.path.exists('./Model_Result/test_files'):
            os.makedirs('./Model_Result/test_files')
    def save_model_mode(model, safe_mode, save_freq, i):
        '''
        儲存model的模式設定：safe_mode == 0 代表儲存多個模型；safe_mode == 1 儲存單一模型
        此區域上未完工！！
        '''
        if args.save_state_dict:
            if safe_mode == 0:
                # 儲存多個模型(只要比前面好就儲存)
                save_name = f"{args.direc}/num{i // save_freq}_model.pth"
                torch.save(model.state_dict(), save_name)
                print(f'best_model is in {i} epoch', f"num{i // save_freq}_model.pt are saved!", sep='\t')
            if safe_mode == 1:
                # 儲存單個模型(只儲存最好的)
                save_name = f"{args.direc}/best_model.pth"
                torch.save(model.state_dict(), save_name)
                print(f'best_model is in {i} epoch', f"best_model.pt are saved!", sep='\t')
        else:
            if safe_mode == 0:
                # 儲存多個模型(只要比前面好就儲存)
                save_name = f"{args.direc}/num{i // save_freq}_model.pth"
                torch.save(model.state_dict(), save_name)
                print(f'best_model is in {i} epoch', f"num{i // save_freq}_model.pt are saved!", sep='\t')
            if safe_mode == 1:
                # 儲存單個模型(只儲存最好的)
                save_name = f"{args.direc}/best_model.pth"
                torch.save(model.state_dict(), save_name)
                print(f'best_model is in {i} epoch', f"best_model.pt are saved!", sep='\t')
    def model_output_dim_adjust(model_output):
        mo = model_output
        ch, size = 2, args.imgsize # use 2 if MedT, use 1 if timm(ViT)
        if mo.shape == (1, ch, size, size):
            return mo
        elif mo.dim() == 3:
            assert len(mo) == ch
            mo = mo.squeeze(0) # add batch dim
            return mo
        elif mo.dim() == 2:
            assert len(mo) == 1 and len(mo[0]) != 0
            mo = mo.view(1, 1, size, size)
            return mo



    # =============================額外增加功能終止線=============================

    warnings.filterwarnings("ignore", category=UserWarning)  # supress Userwarning
    main(args)




'''-------------------------------------------------------'''
# train_dataset = "D:\Programming\AI&ML\(Dataset)Gland Segmentation in Colon Histology Images Challenge\dataset"
# val_dataset = "D:\Programming\AI&ML\(Dataset)Gland Segmentation in Colon Histology Images Challenge\val_dataset"