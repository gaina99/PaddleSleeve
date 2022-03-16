# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
The Target Patch ETO Attack implementation.
Contains:
* Initialize a ppyolo, a yolov3 detector and inference pictures.
* Generate adversarial patch using the two given model weights.
* Generate adversarial images.

Author: tianweijuan
"""
## ignore warning log
from __future__ import absolute_import
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname("__file__"),os.path.pardir)))
import warnings
warnings.filterwarnings('ignore')
import glob
import cv2
import math
from PIL import Image

import xmltodict
import numpy
import paddle
import paddle.nn.functional as F
from ppdet.core.workspace import create
from ppdet.core.workspace import load_config, merge_config
from ppdet.engine import Trainer
from ppdet.utils.check import check_gpu, check_version, check_config
from ppdet.utils.cli import ArgsParser
from ppdet.slim import build_slim_model
from ppdet.utils.visualizer import visualize_results, save_result
from ppdet.data.source.category import get_categories
from ppdet.metrics import get_infer_results
from ppdet.utils.logger import setup_logger

from depreprocess.operator_composer import OperatorCompose
from eot_simulation import transformation


logger = setup_logger('train')


def parse_args():
    parser = ArgsParser()
    parser.add_argument(
        "--infer_dir",
        type=str,
        default=None,
        help="Directory for images to perform inference on.")
    parser.add_argument(
        "--infer_img",
        type=str,
        default=None,
        help="Image path, has higher priority over --infer_dir")
    parser.add_argument(
        "--target_img",
        type=str,
        default=None,
        help="Image path, infer image with masked on.")
    parser.add_argument(
        "--output_dir",
        type=str,
        default="output",
        help="Directory for storing the output visualization files.")
    parser.add_argument(
        "--draw_threshold",
        type=float,
        default=0.6,
        help="Threshold to reserve the result for visualization.")
    parser.add_argument(
        "--slim_config",
        default=None,
        type=str,
        help="Configuration file of slim method.")
  
    parser.add_argument(
        "--use_vdl",
        type=bool,
        default=False,
        help="Whether to record the data to VisualDL.")
    parser.add_argument(
        '--vdl_log_dir',
        type=str,
        default="vdl_log_dir/image",
        help='VisualDL logging directory for image.')
    parser.add_argument(
        "--save_txt",
        type=bool,
        default=False,
        help="Whether to save inference result in txt.")
    args = parser.parse_args()
    return args


def get_test_images(infer_dir, infer_img):
    """
    Get image path list in TEST mode
    """
    assert infer_img is not None or infer_dir is not None, \
        "--infer_img or --infer_dir should be set"
    assert infer_img is None or os.path.isfile(infer_img), \
            "{} is not a file".format(infer_img)
    assert infer_dir is None or os.path.isdir(infer_dir), \
            "{} is not a directory".format(infer_dir)

    # infer_img has a higher priority
    if infer_img and os.path.isfile(infer_img):
        return [infer_img]

    images = set()
    infer_dir = os.path.abspath(infer_dir)
    assert os.path.isdir(infer_dir), \
        "infer_dir {} is not a directory".format(infer_dir)
    exts = ['jpg', 'jpeg', 'png', 'bmp']
    exts += [ext.upper() for ext in exts]
    for ext in exts:
        images.update(glob.glob('{}/*.{}'.format(infer_dir, ext)))
    images = list(images)

    assert len(images) > 0, "no image found in {}".format(infer_dir)
    logger.info("Found {} inference images in total.".format(len(images)))

    return images


def get_pcls(model, neck_feats):
    """
    Get pcls with given neck_feats.
    Args:
        model: paddle.model. PaddleDetection model.
        neck_feats: paddle.tensor. Inferenced result from detector.head.

    Returns:
        paddle.tensor. pcls tensor.
    """
   
    pcls_list = []
    for i, feat in enumerate(neck_feats):
        yolo_output = model.yolo_head.yolo_outputs[i](feat)
        
        if model.data_format == 'NHWC':
            yolo_output = paddle.transpose(yolo_output, [0, 3, 1, 2])
        p = yolo_output
        number_anchor = 3
        b, c, h, w = p.shape

        p = p.reshape((b, number_anchor, -1, h, w)).transpose((0, 1, 3, 4, 2))
        _, pcls = p[:, :, :, :, 4:5], p[:, :, :, :, 5:]
        pcls_list.append(pcls)

    return pcls_list


def get_mask_coordination(_object):
    """
    Place mask coordination in variables.

    Args:
    maskfilename: Path for the xml file containing mask coordination.
    **kwargs: Other named arguments.
    """

    xmin = int(_object['bndbox']['xmin'])
    ymin = int(_object['bndbox']['ymin'])
    xmax = int(_object['bndbox']['xmax'])
    ymax = int(_object['bndbox']['ymax'])

    return xmin,ymin,xmax,ymax


class AttackNet(paddle.fluid.dygraph.Layer):
    """
    The attack_net implementation based on PaddlePaddle.
    As mentioned in the original paper, author proposes a novel expectation over transformation
    method that automatically learns the adversarial patch foucs on the original input image. 
    The model aims to remain adversarial under mage transformations that occur in the real world.
    The original article refers to
    Athalye, A, et, al. "Synthesizing Robust Adversarial Examples."
    (http://proceedings.mlr.press/v80/athalye18b/athalye18b.pdf).
    Args:
        cfg (dict): The model definition to be attacked.
        dic (dict): The added patch size and object label definition.
    """

    def __init__(self, trainer1, trainer2, dic, target_size):
        super(AttackNet, self).__init__()
        self.trainer1 = trainer1
        self.trainer2 = trainer2
        self.label_id = int(dic['annotation']['label']['id'])
        mask_list = dic['annotation']['object']
        box_list = dic['annotation']['size']
        widtht, heightt = int(box_list['width']), int(box_list['height'])
        xmin, ymin, xmax, ymax = get_mask_coordination(mask_list[0])
        self.h, self.w = target_size
        self.xmin_1 = math.floor(self.w/widtht * xmin) # floor
        self.ymin_1 = math.floor(self.h/heightt * ymin) # floor
        self.xmax_1 = math.ceil(self.w/widtht * xmax) # ceil
        self.ymax_1 = math.ceil(self.h/heightt * ymax) # ceil
        height = self.ymax_1 - self.ymin_1
        width = self.xmax_1 - self.xmin_1      
        self.init_inter_mask = paddle.fluid.initializer.Normal(loc=0.0, scale=0.75) 
        self.masked_inter = paddle.fluid.layers.create_parameter([1, 3, height, width], 'float32', name="masked_inter", default_initializer=self.init_inter_mask)
        EOT_transforms = transformation.target_sample()
        self.num_of_EOT_transforms = len(EOT_transforms)
        self.transform = numpy.array(EOT_transforms).reshape(((94, 2, 3)))
        self.transform = paddle.to_tensor(self.transform, dtype= 'float32') 
        self.nnSigmoid = paddle.nn.Sigmoid()
       
    @paddle.no_grad()
    def ext_out(self, input_data, constrained):
        input_data['image'][:, :, self.ymin_1:self.ymax_1, self.xmin_1:self.xmax_1]= constrained[0:1, :, self.ymin_1:self.ymax_1, self.xmin_1:self.xmax_1]
        outs1 = self.trainer1.model(input_data)
        outs2 = self.trainer2.model(input_data)
        return outs1, outs2, input_data['image'][0].detach()
    
    def forward(self, input1, input2):     
        useEOT = True
        if useEOT == True:
            masked_inter_batch = self.masked_inter
            
            for i in range(self.num_of_EOT_transforms):
                if i == self.num_of_EOT_transforms-1: break
                masked_inter_batch = paddle.concat([masked_inter_batch, self.masked_inter],0)
        else:
            masked_inter_batch = self.masked_inter
        X = input1['image'].detach()
        X_batch = X            
        for i in range(self.num_of_EOT_transforms-1):
            X_batch = paddle.concat([X_batch, X],0) 
        
        masked_inter_batch_val = paddle.clip(masked_inter_batch, min=-5.0, max=5.0)
        left1 = X_batch[:, :, self.ymin_1:self.ymax_1, 0:self.xmin_1]
        right1 = X_batch[:, :, self.ymin_1:self.ymax_1, self.xmax_1:self.w]
        bottom1 = X_batch[:, :, 0:self.ymin_1, :]
        top1 = X_batch[:, :, self.ymax_1:self.h, :]
    
        shuru = paddle.concat([left1, masked_inter_batch_val], axis =3)
        shuru = paddle.concat([shuru, right1], axis =3)
        shuru = paddle.concat([bottom1, shuru], axis=2)
        shuru = paddle.concat([shuru, top1], axis=2)
        grid = paddle.nn.functional.affine_grid(self.transform, shuru.shape)
        shuru = paddle.nn.functional.grid_sample(shuru, grid, mode='bilinear')
        constrained = shuru
        constrained[:, :, self.ymin_1:self.ymax_1, self.xmin_1:self.xmax_1] = paddle.tanh(constrained[:, :, self.ymin_1:self.ymax_1, self.xmin_1:self.xmax_1])       
        
        attack_region = X_batch[:, :, self.ymin_1:self.ymax_1, self.xmin_1:self.xmax_1]
        adverse_region = constrained[:, :, self.ymin_1:self.ymax_1, self.xmin_1:self.xmax_1]
        perturbation = attack_region - adverse_region
        distance_L21 = paddle.norm(perturbation, p=2, axis=2) 
        distance_L22 = paddle.norm(distance_L21, p=2, axis=2) 
        distance_L23 = paddle.norm(distance_L22, p=2, axis=1) 
        distance_L2 = paddle.fluid.layers.reduce_mean(distance_L23, dim=0)
    
        lala1 = adverse_region[:, :, 0:-1, 0:-1]
        lala2 = adverse_region[:, :, 1:, 1:]

        sub_lala1_2 = lala1-lala2
        non_smoothness1 = paddle.norm(sub_lala1_2, p=2, axis=2)
        non_smoothness2 = paddle.norm(non_smoothness1, p=2, axis=2)
        non_smoothness3 = paddle.norm(non_smoothness2, p=2, axis=1)
        non_smoothness = paddle.fluid.layers.reduce_mean(non_smoothness3, dim=0)
        input2['image'] = constrained
        self.trainer1.model.eval()
        self.trainer2.model.eval()
 
        body_feats1 = self.trainer1.model.backbone(input2)
        outs21 = self.trainer1.model.neck(body_feats1, False)
        body_feats2 = self.trainer2.model.backbone(input2)
        outs22 = self.trainer2.model.neck(body_feats2, False)
        pcls_list1 = get_pcls(self.trainer1.model, outs21)
        pcls_list2 = get_pcls(self.trainer2.model, outs22)
        C_target1 = 0.
        C_target2 = 0.
        C_nontarget1 = 0.
        C_nontarget2 = 0.
        for pcls1, pcls2 in zip(pcls_list1, pcls_list2):
            b, anc, h, w, cls = pcls1.shape
            pcls1 = self.nnSigmoid(pcls1)
            pcls2 = self.nnSigmoid(pcls2)
            
            x11 =  pcls1[:, :, :, :, 0:3]
            x21 = pcls1[:, :, :, :, 5:8] 
            x31 = pcls1[:, :, :, :, 9:]
            x1 = paddle.concat([x11, x21, x31], axis = -1)
            x1 = paddle.fluid.layers.reduce_max(x1, dim=-1)
            x1 = paddle.reshape(x1, [b, anc*h*w])
            x1, _ = (paddle.topk(x1, 3, axis=1))
            x1 = paddle.fluid.layers.reduce_sum(x1, 1)
            x1 = paddle.fluid.layers.reduce_sum(x1, 0)
            
            x12 =  pcls2[:, :, :, :, 0:3]
            x22 = pcls2[:, :, :, :, 5:8]
            x32 = pcls2[:, :, :, :, 9:]
            x2 = paddle.concat([x12, x22, x32], axis = -1)
            x2 = paddle.fluid.layers.reduce_max(x2, dim=-1)
            x2 = paddle.reshape(x2, [b, anc*h*w])
            x2, _ = (paddle.topk(x2, 3, axis=1))
            x2 = paddle.fluid.layers.reduce_sum(x2, 1) 
            x2 = paddle.fluid.layers.reduce_sum(x2, 0)

            C_nontarget1 += x1
            C_nontarget2 += x2
           
            pcls_31 = paddle.reshape(pcls1[:, :, :, :, 3], [b, anc*h*w])
            pcls_31 = paddle.fluid.layers.reduce_max(pcls_31, 1) 
            pcls_31 = paddle.fluid.layers.reduce_sum(pcls_31, 0) # b, 1
            C_target1 += 0.8 *pcls_31 
            pcls_32 = paddle.reshape(pcls2[:, :, :, :, 3], [b, anc*h*w])
            pcls_32 = paddle.fluid.layers.reduce_max(pcls_32, 1)
            pcls_32 = paddle.fluid.layers.reduce_sum(pcls_32, 0) # b, 1
            C_target2 += 0.8 *pcls_32
        
        C_target = 0.55*C_target1 + 0.45*C_target2  # weight to be modified
        C_nontarget = 0.55*C_nontarget1 + 0.45*C_nontarget2 # weight to be modified   
        
        punishment = 0.0001
        smoothness_punishment = 0.005
        loss = (C_nontarget - C_target)/94. + punishment* distance_L2 + smoothness_punishment* non_smoothness

        outs_adv1, outs_adv2, in_adv = self.ext_out(input1, constrained)  
        return loss, outs_adv1, outs_adv2, in_adv

      
def run(FLAGS, cfg1, cfg2, trainer1, trainer2):
    """
    construct input data and call the AttackNet to achieve the adversarial patch learning.
    Args:
        FLAGS(dict): configure parameters
        cfg(str): attacked model configs
    """

    f = open('patch_def/EOTB_car.xml')
    dic = xmltodict.parse(f.read())
    size = dic['annotation']['size']
    depre_settings = {'ImPermute': {},
                      'DenormalizeImage': {'mean': [0.485, 0.456, 0.406], 'std': [0.229, 0.224, 0.225],
                                           'input_channel_axis': 2, 'is_scale': True},
                      'Resize': {'target_size': (int(size['height']), int(size['width']), int(size['depth'])), 'keep_ratio': False, 'interp': 2}, 
                      'Encode': {}
                      }
    depreprocessor = OperatorCompose(depre_settings)
    
    data0, datainfo0  = _image2outs(FLAGS.infer_dir, FLAGS.infer_img, cfg1)
     
    epochs = 800
    target_size = cfg1['EvalReader']['sample_transforms'][1]['Resize']['target_size']
    model_attack = AttackNet(trainer1, trainer2, dic, target_size)
    scheduler = paddle.optimizer.lr.CosineAnnealingDecay(learning_rate=0.01, T_max=800, verbose=True)
    data, _ = batchloader(FLAGS.infer_dir, FLAGS.infer_img, cfg1)

    opt = paddle.optimizer.Adam(learning_rate= scheduler, parameters = model_attack.parameters())  
    for epoch in range (epochs):       
        loss, outs_adv1, outs_adv2, data_adv = model_attack(data0, data) 
        loss.backward()   
        opt.minimize(loss)
        print('Epoch:', epoch, '======loss:', loss.numpy())
        flag1 = ext_score(outs_adv1, data0, datainfo0, int(dic['annotation']['label']['id']))
        flag2 = ext_score(outs_adv2, data0, datainfo0, int(dic['annotation']['label']['id']))
        if flag1 and flag2:
            break
    
    data_adv = depreprocessor(data_adv)  

    if not os.path.exists(FLAGS.output_dir):
        os.makedirs(FLAGS.output_dir)
    patch_size = dic['annotation']['object'][0]['bndbox']
    xmin, ymin, xmax, ymax = int(patch_size['xmin']), int(patch_size['ymin']), int(patch_size['xmax']), int(patch_size['ymax'])
    adv_patch = data_adv[ymin:ymax, xmin:xmax, :]
    cv2.imwrite(FLAGS.output_dir+'/yolo_ensemble_adverse_'+ FLAGS.infer_img.split('/')[-1], data_adv)    
    cv2.imwrite(FLAGS.output_dir+'/yolo_ensemble_advpatch_'+ FLAGS.infer_img.split('/')[-1], adv_patch)   
    
def batchloader(infer_dir, infer_img, cfg):
    """
    get input batch data to construct expectation data for adversarial patch learning.
    Args:
        infer_dir(str): input data path 
        infer_img(str): input data filename
        cfg(dict): attacked model definition
    """

    mode = 'test'  
    dataset = cfg['{}Dataset'.format(mode.capitalize())]
    for i in range(94):
        images = get_test_images(infer_dir, infer_img)
        dataset.set_images(images*(i+1))
       
    imid2path = dataset.get_imid2path
    anno_file = dataset.get_anno()
    clsid2catid, catid2name = get_categories(cfg.metric, anno_file=anno_file)
    datainfo = {'imid2path': imid2path,
                'clsid2catid': clsid2catid,
                'catid2name': catid2name}
    _eval_batch_sampler = paddle.io.BatchSampler(
                dataset, batch_size=94)
    loader = create('{}Reader'.format(mode.capitalize()))(
                dataset, 2, _eval_batch_sampler)
    for _, data in enumerate(loader):
        break
    
    return data, datainfo
    

def _image2outs(infer_dir, infer_img, cfg):
    """
    construct the single input data for the post data process.
    Args:
        infer_dir(str): input data path 
        infer_img(str): input data filename
        cfg(dict): attacked model definition
    """

    print(cfg['EvalReader']['sample_transforms'])
    print(cfg['EvalReader']['sample_transforms'][1]['Resize']['target_size'])
    mode = 'test'
    dataset = cfg['{}Dataset'.format(mode.capitalize())]
    
    images = get_test_images(infer_dir, infer_img)
    dataset.set_images(images*1)

    loader = create('TestReader')(dataset, 0)
    imid2path = dataset.get_imid2path
    anno_file = dataset.get_anno()
    clsid2catid, catid2name = get_categories(cfg['metric'], anno_file=anno_file)
    datainfo = {'imid2path': imid2path,
                'clsid2catid': clsid2catid,
                'catid2name': catid2name}

    # loader have 1 images, step_id=0, data contain bbox, bbox_num, neck_feats
    for _, data in enumerate(loader):
        break
        
    return data, datainfo 


def ext_score(outs, data, datainfo, label_id):
    """
    extract the detection score of the learned adversarial sample.
    Args:
        outs(dict): output of the learned adversarial sample 
        data(dict): the learned adversarial sample
        datainfo(dict): data information of the specific dataset
        label_id(int): the original target class id 
    """

    clsid2catid = datainfo['clsid2catid']
    catid2name = datainfo['catid2name']
    for key in ['im_shape', 'scale_factor', 'im_id']:
        outs[key] = data[key]
    for key, value in outs.items():
        if hasattr(value, 'numpy'):
            outs[key] = value.numpy()

    batch_res = get_infer_results(outs, clsid2catid)
    start = 0
    flag = True

    bbox_num = outs['bbox_num']
    for i, im_id in enumerate(outs['im_id']):
        end = start + bbox_num[i]
        bbox_res = batch_res['bbox'][start:end]
        for dt in numpy.array(bbox_res):
            catid, bbox, score = dt['category_id'], dt['bbox'], dt['score']
            if catid == label_id  and score > 0.58:
                flag = False
            if score >= 0.5:
                print(catid, catid2name[catid], score) 
        if flag:
            return True    
    return False


def test():
    FLAGS = parse_args()
    FLAGS.config1, FLAGS.config2 = FLAGS.config.split(',')
    FLAGS.opt1, FLAGS.opt2 = str(list(FLAGS.opt.values()))[2:-2].split(',')
    
    cfg1 = load_config(FLAGS.config1)  
    cfg1 = load_config(FLAGS.config1)
        
    cfg1['use_vdl'] = FLAGS.use_vdl
    cfg1['vdl_log_dir'] = FLAGS.vdl_log_dir
    
    FLAGS.opt = {'weights':FLAGS.opt1}
    merge_config(FLAGS.opt)
    place = paddle.set_device('gpu' if cfg1.use_gpu else 'cpu')
    if 'norm_type' in cfg1 and 'norm_type' in cfg1 and cfg1['norm_type'] == 'sync_bn' and not cfg1.use_gpu:
        cfg1['norm_type'] = 'bn'
    if FLAGS.slim_config:
        cfg1 = build_slim_model(cfg1, FLAGS.slim_config, mode='test')
    check_config(cfg1)
    check_gpu(cfg1.use_gpu)
    check_version()

    trainer1 = Trainer(cfg1, mode='test')  
    trainer1.load_weights(cfg1.weights)
   
    paddle.fluid.dygraph.disable_dygraph()
    paddle.fluid.dygraph.enable_dygraph()
    
    cfg2 = load_config(FLAGS.config2)
    cfg2['use_vdl'] = FLAGS.use_vdl
    cfg2['vdl_log_dir'] = FLAGS.vdl_log_dir
    FLAGS.opt = {'weights':FLAGS.opt2}
    merge_config(FLAGS.opt)
    place = paddle.set_device('gpu' if cfg2.use_gpu else 'cpu')

    if 'norm_type' in cfg2 and cfg2['norm_type'] == 'sync_bn' and not cfg2.use_gpu:
        cfg2['norm_type'] = 'bn'

    if FLAGS.slim_config:
        cfg2 = build_slim_model(cfg2, FLAGS.slim_config, mode='test')
    
    check_config(cfg2)
    check_gpu(cfg2.use_gpu)
    
    check_version()
    
    trainer2 = Trainer(cfg2, mode='test')
    trainer2.load_weights(cfg2.weights)
    run(FLAGS, cfg1, cfg2, trainer1, trainer2)


if __name__ == '__main__':
    test()
