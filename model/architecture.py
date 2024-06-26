import os
import numpy as np
import torch
from torch import optim

from model.utils import Cos_loss, GAN_loss, ImagePool, get_ee, Criterion_EE, Eval_Criterion, Criterion_EE_2
from model.base_model import BaseModel
from model.intergrated import IntegratedModelGIP
from option_parser import try_mkdir
import articulate as art
import config as conf


class GAN_model_GIP(BaseModel):
    def __init__(self, args, dataset, epoch_k=4, std_paths=None, log_path=None):
        super(GAN_model_GIP, self).__init__(args, log_path=log_path)
        self.device = torch.device(args.cuda_device if (torch.cuda.is_available()) else 'cpu')
        self.character_names = ['Smpl']
        self.dataset = dataset
        self.args = args
        self.std_path = std_paths
        self.epochCount = -1
        self.epoch_k = epoch_k

        model_GIP = IntegratedModelGIP(args)
        self.models = model_GIP
        self.D_para = model_GIP.D_parameters()
        self.G_para = model_GIP.G_parameters()

        self.criterion_rec = torch.nn.MSELoss()
        # self.optimizerD = optim.RMSprop(self.D_para, args.learning_rate / 20.0) # /20.0
        # self.optimizerG = optim.RMSprop(self.G_para, args.learning_rate / 10.0) # 不处理
        self.optimizerD = optim.Adam(self.D_para, args.learning_rate / 2.0, betas=(0.9, 0.999))
        self.optimizerG = optim.Adam(self.G_para, args.learning_rate / 2.0, betas=(0.9, 0.999))
        self.optimizers = [self.optimizerD, self.optimizerG]
        if args.gan_mode != 'finetune':
            self.criterion_gan = GAN_loss(args.gan_mode).to(self.device)
        self.criterion_cos = Cos_loss().to(self.device)
        # self.criterion_cycle = torch.nn.L1Loss()
        # self.criterion_ee = Criterion_EE(args, torch.nn.MSELoss())
        self.fake_pools = ImagePool(args.pool_size)
        
        self.smpl_model_func = art.ParametricModel(conf.paths.smpl_file)
        
        self.rec_loss_val_store = 0

    def set_input(self, motions):
        self.motions_input = motions    # 包括了[2, motion, character], 其中、motion:tensor[1,C,t_w], character:一个int

    def discriminator_requires_grad_(self, requires_grad):
        # for model in self.models:
        model = self.models
        for para in model.discriminator.parameters():
            para.requires_grad = requires_grad

    def generator_requires_grad_(self, requires_grad):
        # for model in self.models:
        model = self.models
        for para in model.auto_encoder.parameters():
            para.requires_grad = requires_grad

    def forward(self):
        self.epochCount += 1
        imus, joints, poseGT, poseGAN, shape = self.motions_input  # [n,t,C(24*3+72)]
        
        n,t,_ = joints.shape
        joints = joints.view(n,t,23,3)
        leaf = [7-1, 8-1, 12-1, 20-1, 21-1]
        self.gt_pos_leaf = joints[:,:,leaf].view(n,t,15)
        self.gt_pos_all = joints.view(n,t,69)
        self.gt_pose = poseGT   # [n,t,90]
        # self.gt_pose = poseGT.view(n,t,15*9)   #[n,t,135]
        # self.gt_pose_ganRef = poseGAN
        
        
        # 网络整体训练
        # leaf_pos, all_pos, r6dpose = self.models.auto_encoder.calSMPLpose(imus, acc_scale=True)   # transpose
        # leaf_pos, all_pos, r6dpose = self.models.auto_encoder.calSMPLpose(imus, self.gt_pos_leaf[:,0])  # PIP
        leaf_pos, all_pos, r6dpose = self.models.pose_encoder.forwardRaw(imus)  # GAIP
        self.res_pos_leaf = leaf_pos    #[n,t,15]
        self.res_pos_all = all_pos      #[n,t,69]
        self.res_pose = r6dpose     #[n,t,90]
        
        # r6dpose = self.models.pose_encoder.ggip3ForwardRaw(imus, joints)
        # self.res_pose = r6dpose     #[n,t,15*9=135]
        
        # matpose, eulerpose = self.models.pose_encoder.ggip3ForwardRaw(imus, joints)
        # self.res_pose_euler = eulerpose
        # self.res_pose = matpose     #[n,t,15*9=135]
        
        self.shape = shape[:,0]  # [n,t,10] => [n,10]


    def backward_D_basic(self, netD, real, fake):
        """Calculate GAN loss for the discriminator
        GAN网络中判别器D的反向传播！
        Parameters:
            netD (network)      -- the discriminator D
            real (tensor array) -- real images
            fake (tensor array) -- images generated by a generator
        Return the discriminator loss.
        We also call loss_D.backward() to calculate the gradients.
        """
        # Real
        pred_real = netD(real)
        loss_D_real = self.criterion_gan(pred_real, True)
        # Fake
        pred_fake = netD(fake.detach())     # 通过detach断开了反向传播链，所以前面生成器的参数不会更新！
        loss_D_fake = self.criterion_gan(pred_fake, False)
        # Combined loss and calculate gradients
        loss_D = (loss_D_real + loss_D_fake) * 0.5
        loss_D.backward()
        return loss_D

    def backward_D(self):
        self.loss_Ds = []
        self.loss_D = 0
        """
        A->A
        """
        # for i in range(self.n_topology):
        fake = self.fake_pools.query(self.res_pose)
        self.loss_Ds.append(self.backward_D_basic(self.models.discriminator, self.gt_pose.detach(), fake))
        # self.loss_Ds.append(self.backward_D_basic(self.models.discriminator, self.gt_pose_ganRef.detach(), fake))
        self.loss_D += self.loss_Ds[-1]
        self.loss_recoder.add_scalar('D_loss', self.loss_Ds[-1])

    def backward_G(self, backward=True):
        self.loss_G = 0

        r'''生成器计算损失 & 反向传播'''
        #rec_loss and gan loss

        # 重建损失 L_rec
        # for i in range(self.n_topology):
        rec_loss = self.criterion_rec(self.gt_pose, self.res_pose)
        self.loss_recoder.add_scalar('rec_loss_r6d', rec_loss)
        self.rec_loss = rec_loss
        
        # _,gt_fk_pose = self.reduced_local_to_global(self.gt_pose.shape[0], self.gt_pose.shape[1], self.gt_pose, self.shape)
        # _,res_fk_pose = self.reduced_local_to_global(self.gt_pose.shape[0], self.gt_pose.shape[1], self.res_pose, self.shape)
        # posFK_loss = self.criterion_rec(gt_fk_pose, res_fk_pose)
        # self.loss_recoder.add_scalar('rec_loss_posFK', posFK_loss)
        # self.posFK_loss = posFK_loss
        
        # gt_mat_pose = art.math.r6d_to_rotation_matrix(self.gt_pose).view(self.gt_pose.shape[0], self.gt_pose.shape[1], 15*9)
        # res_mat_pose = art.math.r6d_to_rotation_matrix(self.res_pose).view(self.gt_pose.shape[0], self.gt_pose.shape[1], 15*9)
        
        # cos_loss = self.criterion_cos(gt_mat_pose, res_mat_pose)
        # self.loss_recoder.add_scalar('cos_loss', cos_loss)
        # self.cos_loss = cos_loss
        
        # ee_loss = self.criterion_rec(self.gt_pos_leaf, self.res_pos_leaf)
        # self.loss_recoder.add_scalar('ee_loss', ee_loss)
        # self.ee_loss = ee_loss
        # pos_loss = self.criterion_rec(self.gt_pos_all, self.res_pos_all)
        # self.loss_recoder.add_scalar('pos_loss', pos_loss)
        # self.pos_loss = pos_loss
        
        # consistent_loss = self.criterion_rec(self.res_pose[:,1:], self.res_pose[:,:-1])     # 缩小跟前一帧的差异，减小抖动
        # consistent_loss = self.criterion_rec(res_fk_pose[:,1:], res_fk_pose[:,:-1])     # 缩小跟前一帧的差异，减小抖动
        # self.loss_recoder.add_scalar('consistent_loss', consistent_loss)
        # self.consis_loss = consistent_loss
        
        
        # GAN损失，应该指的是输出的判别结果与标注（只训练生成器时标注为True）之间的损失差 -> 就是L_adv
        if self.args.gan_mode == 'lsgan':
            loss_G = self.criterion_gan(self.models.discriminator(self.res_pose), True)
        elif self.args.gan_mode == 'finetune':
            loss_G = torch.tensor(0).float().to(self.device)
            G_para_tmp = self.models.G_parameters()
            for i in range(len(G_para_tmp)):
                for content, content_origin in zip(G_para_tmp[i], self.G_para_origin[i]):
                    loss_G += 10000.0 * self.criterion_rec(content, content_origin)
        else:
            loss_G = torch.tensor(0)
        self.loss_recoder.add_scalar('G_loss', loss_G)
        self.loss_G += loss_G

        # 通过控制最终反向传播涉及到的损失、来控制训练哪一部分的网络
        # 我们训练的结果训练出来是： 5*40 : 1 : 100 : 0?
        self.loss_G_total = self.rec_loss * self.args.lambda_rec * 60 + \
                            self.loss_G * 0.5# + \
                            # self.posFK_loss * 200# + \
                            # self.consis_loss * 50      # (1)4*50,1,50,50 (2)4*40,0.5,30,50
        #                     self.ee_loss * 200 + \
        #                     self.pos_loss * 200
        # loss_G的权重一般都是1，这次dip finetune调整为0.005看看，不然在一开始lossG会处于主导地位
                            
        if backward:
            self.loss_G_total.backward()        # 反向传播

    def optimize_parameters(self):
        r'''正向传播+反向传播的过程'''
        self.forward()

        # update Gs
        # 先更新生成器的参数
        self.discriminator_requires_grad_(False)    # 停止判别器的参数更新
        self.optimizerG.zero_grad()
        self.backward_G()                           # 计算论文中提到的4项损失，进行反向推导
        self.optimizerG.step()

        # update Ds
        # 再更新判别器的参数
        if self.args.gan_mode != 'none' and self.args.gan_mode != 'finetune' and self.epochCount % self.epoch_k == 0:
            self.discriminator_requires_grad_(True) # 开始判别器参数更新（并没有停止生成器参数的更新？）
            self.optimizerD.zero_grad()
            self.backward_D()
            self.optimizerD.step()
        else:
            if self.args.gan_mode == 'none' or self.args.gan_mode == 'finetune':
                self.loss_D = torch.tensor(0)
            self.loss_recoder.add_scalar('D_loss', self.loss_D)
            # self.loss_D = torch.tensor(0)   # 不使用GAN，则不训练、也不更新判别器的参数

    def verbose(self):
        res = {'rec_loss': self.rec_loss.item(),
            #    'posFK_loss': self.posFK_loss.item(),
            #    'cos_loss': self.cos_loss.item(),
            #    'pos_loss': self.pos_loss.item(),
            #    'ee_loss': self.ee_loss.item(),
            #    'consistent_loss': self.consis_loss.item(),
               'D_loss_gan': self.loss_D.item(),
               'G_loss_gan': self.loss_G.item()
               }
        return sorted(res.items(), key=lambda x: x[0])

    def save(self, suffix=None):
        if suffix:
            self.model_save_dir = str(suffix)
        # for i, model in enumerate(self.models):
        self.models.save(os.path.join(self.model_save_dir, 'topology'), self.epoch_cnt)

        for i, optimizer in enumerate(self.optimizers):
            file_name = os.path.join(self.model_save_dir, 'optimizers/{}/{}.pt'.format(self.epoch_cnt, i))
            try_mkdir(os.path.split(file_name)[0])
            torch.save(optimizer.state_dict(), file_name)

    def load(self, epoch=None, suffix=None, loadOpt=True):
        # for i, model in enumerate(self.models):
        if suffix:
            self.model_save_dir = str(suffix)
            
        self.models.load(os.path.join(self.model_save_dir, 'topology'), epoch)

        # 换了优化器，所以不加载原本的
        if self.is_train and loadOpt:
            for i, optimizer in enumerate(self.optimizers):
                file_name = os.path.join(self.model_save_dir, 'optimizers/{}/{}.pt'.format(epoch, i))
                optimizer.load_state_dict(torch.load(file_name))
        self.epoch_cnt = epoch + 1
        
        if self.args.gan_mode == 'finetune':
            self.G_para_origin = []
            for i in range(len(self.models.G_parameters())):
                self.G_para_origin.append(self.models.G_parameters()[i].clone().to(self.device).detach())


    def SMPLtest(self, motions_input):
        with torch.no_grad():
            if self.is_train:
                imu, joint, motion, root = motions_input  # [n,C(4v-4+3),t_w(64)],  一个int对应character的序号
                # motion = motion.view(motion.shape[0], motion.shape[1], 15*9)
                
                leafpos, allpos, res = self.testForward(imu) # GAIP & transpose
                # leafpos, allpos, res = self.testForward(imu, initPose=motion[0]) # PIP
                rec_loss = self.testLoss(motion, res, joint, leafpos, allpos)
            else:
                imu, motion, root = motions_input
                _,_,res = self.testForward(imu) # GAIP & transpose
                # _,_,res = self.testForward(imu, initPose=motion[0]) # PIP
                rec_loss = self.testLoss(motion, res)
            
            # GAIP
            smplPoseGT = self.models.pose_encoder._reduced_glb_6d_to_full_local_mat(root, motion)
            smplPoseRes = self.models.pose_encoder._reduced_glb_6d_to_full_local_mat(root, res)
            # transpose & PIP
            # smplPoseGT = self.models.auto_encoder._reduced_glb_6d_to_full_local_mat(root, motion)
            # smplPoseRes = self.models.auto_encoder._reduced_glb_6d_to_full_local_mat(root, res)
            return rec_loss, smplPoseGT, smplPoseRes
        
    def testForward(self, input, initPose=None, joint=None):
        leafpos, allpos, r6dpose = self.models.pose_encoder.forwardRaw(input)   # GAIP
        # leafpos, allpos, r6dpose = self.models.auto_encoder.calSMPLpose(input, acc_scale=True)   # transpose
        # leafpos, allpos, r6dpose = self.models.auto_encoder.calSMPLpose_eval(input, initPose)   # PIP
        return leafpos, allpos, r6dpose
    
    def testLoss(self, poseGT, poseRes, jointPosGT=None, leafPos=None, allPos=None):
        rec_loss = self.criterion_rec(poseGT, poseRes)
        self.rec_loss_val_store = rec_loss
        if self.args.is_train:
            self.loss_recoder.add_scalar('rec_loss_r6d/val', self.rec_loss_val_store)
            
            # gt_mat_pose = art.math.r6d_to_rotation_matrix(self.gt_pose).view(self.gt_pose.shape[0], self.gt_pose.shape[1], 15*9)
            # res_mat_pose = art.math.r6d_to_rotation_matrix(self.res_pose).view(self.gt_pose.shape[0], self.gt_pose.shape[1], 15*9)
            # cos_loss = self.criterion_cos(gt_mat_pose, res_mat_pose)
            # self.loss_recoder.add_scalar('cos_loss/val', cos_loss)
            
            # n,t,_ = jointPosGT.shape
            # jointPosGT = jointPosGT.view(n,t,23,3)
            # leaf = [7-1, 8-1, 12-1, 20-1, 21-1]
            # leafPosGT = jointPosGT[:,:,leaf].view(n,t,15)
            # allPosGT = jointPosGT.view(n,t,69)
            # ee_loss = self.criterion_rec(leafPosGT, leafPos)
            # self.loss_recoder.add_scalar('ee_loss/val', ee_loss)
            # pos_loss = self.criterion_rec(allPosGT, allPos)
            # self.loss_recoder.add_scalar('pos_loss/val', pos_loss)
            
        return rec_loss
    
    def compute_test_result(self):
        print("tmp useless")
        
    def reduced_local_to_global(self, batch, seq, glb_reduced_pose, shape, root_rotation=None):
        glb_reduced_pose = art.math.r6d_to_rotation_matrix(glb_reduced_pose).view(batch, -1, conf.joint_set.n_reduced, 3, 3)
        global_full_pose = torch.eye(3, device=glb_reduced_pose.device).repeat(batch, glb_reduced_pose.shape[1], 24, 1, 1)
        global_full_pose[:, :, conf.joint_set.reduced] = glb_reduced_pose
        
        pose = global_full_pose.clone()
        for i in range(global_full_pose.shape[0]):
            pose[i] = self.smpl_model_func.inverse_kinematics_R(global_full_pose[i]).view(-1, 24, 3, 3) # 到这一步变成了相对父节点的相对坐标
        pose[:, :, conf.joint_set.ignored] = torch.eye(3, device=pose.device)
        
        if root_rotation is not None:
            pose[:, :, 0:1] = root_rotation.view(batch, -1, 1, 3, 3)       # 第一个是全局根节点方向
        
        pose = pose.view(batch, seq, 24,3,3).contiguous() #[n,t,24,3,3]
        joints_pos = torch.zeros(batch, seq, 24, 3)
        for i in range(pose.shape[0]):
            _, a_joints_pos = self.smpl_model_func.forward_kinematics(pose[i], shape=shape[i])
            joints_pos[i] = a_joints_pos.to(self.device)
            
        pose = pose.view(batch, seq, 24, 3, 3)
        joints_pos = joints_pos.view(batch, seq, 24, 3).contiguous()
        return pose, joints_pos # pose是smpl参数【24维度】，joints_pos是全局关节位置【24维度】

        