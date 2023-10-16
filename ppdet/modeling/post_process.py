# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

import numpy as np
import paddle
import paddle.nn.functional as F
from ppdet.core.workspace import register
from ppdet.modeling.bbox_utils import nonempty_bbox
from .transformers import bbox_cxcywh_to_xyxy

__all__ = ['BBoxPostProcess', 'MaskPostProcess', 'DETRPostProcess']


@register
class BBoxPostProcess(object):
    __shared__ = ['num_classes', 'export_onnx', 'export_eb']
    __inject__ = ['decode', 'nms']

    def __init__(self,
                 num_classes=80,
                 decode=None,
                 nms=None,
                 export_onnx=False,
                 export_eb=False):
        super(BBoxPostProcess, self).__init__()
        self.num_classes = num_classes
        self.decode = decode
        self.nms = nms
        self.export_onnx = export_onnx
        self.export_eb = export_eb

    def __call__(self, head_out, rois, im_shape, scale_factor):
        """
        Decode the bbox and do NMS if needed.

        Args:
            head_out (tuple): bbox_pred and cls_prob of bbox_head output.
            rois (tuple): roi and rois_num of rpn_head output.
            im_shape (Tensor): The shape of the input image.
            scale_factor (Tensor): The scale factor of the input image.
            export_onnx (bool): whether export model to onnx
        Returns:
            bbox_pred (Tensor): The output prediction with shape [N, 6], including
                labels, scores and bboxes. The size of bboxes are corresponding
                to the input image, the bboxes may be used in other branch.
            bbox_num (Tensor): The number of prediction boxes of each batch with
                shape [1], and is N.
        """
        if self.nms is not None:
            bboxes, score = self.decode(head_out, rois, im_shape, scale_factor)
            bbox_pred, bbox_num, _ = self.nms(bboxes, score, self.num_classes)

        else:
            bbox_pred, bbox_num = self.decode(head_out, rois, im_shape,
                                              scale_factor)

        if self.export_onnx:
            # add fake box after postprocess when exporting onnx 
            fake_bboxes = paddle.to_tensor(
                np.array(
                    [[0., 0.0, 0.0, 0.0, 1.0, 1.0]], dtype='float32'))

            bbox_pred = paddle.concat([bbox_pred, fake_bboxes])
            bbox_num = bbox_num + 1

        return bbox_pred, bbox_num

    def get_pred(self, bboxes, bbox_num, im_shape, scale_factor):
        """
        Rescale, clip and filter the bbox from the output of NMS to 
        get final prediction. 

        Notes:
        Currently only support bs = 1.

        Args:
            bboxes (Tensor): The output bboxes with shape [N, 6] after decode
                and NMS, including labels, scores and bboxes.
            bbox_num (Tensor): The number of prediction boxes of each batch with
                shape [1], and is N.
            im_shape (Tensor): The shape of the input image.
            scale_factor (Tensor): The scale factor of the input image.
        Returns:
            pred_result (Tensor): The final prediction results with shape [N, 6]
                including labels, scores and bboxes.
        """
        if self.export_eb:
            # enable rcnn models for edgeboard hw to skip the following postprocess.
            return bboxes, bboxes, bbox_num

        if not self.export_onnx:
            bboxes_list = []
            bbox_num_list = []
            id_start = 0
            fake_bboxes = paddle.to_tensor(
                np.array(
                    [[0., 0.0, 0.0, 0.0, 1.0, 1.0]], dtype='float32'))
            fake_bbox_num = paddle.to_tensor(np.array([1], dtype='int32'))

            # add fake bbox when output is empty for each batch
            for i in range(bbox_num.shape[0]):
                if bbox_num[i] == 0:
                    bboxes_i = fake_bboxes
                    bbox_num_i = fake_bbox_num
                else:
                    bboxes_i = bboxes[id_start:id_start + bbox_num[i], :]
                    bbox_num_i = bbox_num[i]
                    id_start += bbox_num[i]
                bboxes_list.append(bboxes_i)
                bbox_num_list.append(bbox_num_i)
            bboxes = paddle.concat(bboxes_list)
            bbox_num = paddle.concat(bbox_num_list)

        origin_shape = paddle.floor(im_shape / scale_factor + 0.5)

        if not self.export_onnx:
            origin_shape_list = []
            scale_factor_list = []
            # scale_factor: scale_y, scale_x
            for i in range(bbox_num.shape[0]):
                expand_shape = paddle.expand(origin_shape[i:i + 1, :],
                                             [bbox_num[i], 2])
                scale_y, scale_x = scale_factor[i][0], scale_factor[i][1]
                scale = paddle.concat([scale_x, scale_y, scale_x, scale_y])
                expand_scale = paddle.expand(scale, [bbox_num[i], 4])
                origin_shape_list.append(expand_shape)
                scale_factor_list.append(expand_scale)

            self.origin_shape_list = paddle.concat(origin_shape_list)
            scale_factor_list = paddle.concat(scale_factor_list)

        else:
            # simplify the computation for bs=1 when exporting onnx
            scale_y, scale_x = scale_factor[0][0], scale_factor[0][1]
            scale = paddle.concat(
                [scale_x, scale_y, scale_x, scale_y]).unsqueeze(0)
            self.origin_shape_list = paddle.expand(origin_shape,
                                                   [bbox_num[0], 2])
            scale_factor_list = paddle.expand(scale, [bbox_num[0], 4])

        # bboxes: [N, 6], label, score, bbox
        pred_label = bboxes[:, 0:1]
        pred_score = bboxes[:, 1:2]
        pred_bbox = bboxes[:, 2:]
        # rescale bbox to original image
        scaled_bbox = pred_bbox / scale_factor_list
        origin_h = self.origin_shape_list[:, 0]
        origin_w = self.origin_shape_list[:, 1]
        zeros = paddle.zeros_like(origin_h)
        # clip bbox to [0, original_size]
        x1 = paddle.maximum(paddle.minimum(scaled_bbox[:, 0], origin_w), zeros)
        y1 = paddle.maximum(paddle.minimum(scaled_bbox[:, 1], origin_h), zeros)
        x2 = paddle.maximum(paddle.minimum(scaled_bbox[:, 2], origin_w), zeros)
        y2 = paddle.maximum(paddle.minimum(scaled_bbox[:, 3], origin_h), zeros)
        pred_bbox = paddle.stack([x1, y1, x2, y2], axis=-1)
        # filter empty bbox
        keep_mask = nonempty_bbox(pred_bbox, return_mask=True)
        keep_mask = paddle.unsqueeze(keep_mask, [1])
        pred_label = paddle.where(keep_mask, pred_label,
                                  paddle.ones_like(pred_label) * -1)
        pred_result = paddle.concat([pred_label, pred_score, pred_bbox], axis=1)
        return bboxes, pred_result, bbox_num

    def get_origin_shape(self, ):
        return self.origin_shape_list


@register
class DETRPostProcess(object):
    __shared__ = ['num_classes', 'use_focal_loss', 'with_mask']
    __inject__ = []

    def __init__(self,
                 num_classes=80,
                 num_top_queries=100,
                 dual_queries=False,
                 dual_groups=0,
                 use_focal_loss=False,
                 with_mask=False,
                 mask_threshold=0.5,
                 use_avg_mask_score=False,
                 bbox_decode_type='origin'):
        super(DETRPostProcess, self).__init__()
        assert bbox_decode_type in ['origin', 'pad']

        self.num_classes = num_classes
        self.num_top_queries = num_top_queries
        self.dual_queries = dual_queries
        self.dual_groups = dual_groups
        self.use_focal_loss = use_focal_loss
        self.with_mask = with_mask
        self.mask_threshold = mask_threshold
        self.use_avg_mask_score = use_avg_mask_score
        self.bbox_decode_type = bbox_decode_type

    def _mask_postprocess(self, mask_pred, score_pred, index):
        mask_score = F.sigmoid(paddle.gather_nd(mask_pred, index))
        mask_pred = (mask_score > self.mask_threshold).astype(mask_score.dtype)
        if self.use_avg_mask_score:
            avg_mask_score = (mask_pred * mask_score).sum([-2, -1]) / (
                mask_pred.sum([-2, -1]) + 1e-6)
            score_pred *= avg_mask_score

        return mask_pred[0].astype('int32'), score_pred

    def __call__(self, head_out, im_shape, scale_factor, pad_shape):
        """
        Decode the bbox and mask.

        Args:
            head_out (tuple): bbox_pred, cls_logit and masks of bbox_head output.
            im_shape (Tensor): The shape of the input image without padding.
            scale_factor (Tensor): The scale factor of the input image.
            pad_shape (Tensor): The shape of the input image with padding.
        Returns:
            bbox_pred (Tensor): The output prediction with shape [N, 6], including
                labels, scores and bboxes. The size of bboxes are corresponding
                to the input image, the bboxes may be used in other branch.
            bbox_num (Tensor): The number of prediction boxes of each batch with
                shape [bs], and is N.
        """
        bboxes, logits, masks = head_out
        if self.dual_queries:
            num_queries = logits.shape[1]
            logits, bboxes = logits[:, :int(num_queries // (self.dual_groups + 1)), :], \
                             bboxes[:, :int(num_queries // (self.dual_groups + 1)), :]

        bbox_pred = bbox_cxcywh_to_xyxy(bboxes)
        # calculate the original shape of the image
        origin_shape = paddle.floor(im_shape / scale_factor + 0.5)
        img_h, img_w = paddle.split(origin_shape, 2, axis=-1)
        if self.bbox_decode_type == 'pad':
            # calculate the shape of the image with padding
            out_shape = pad_shape / im_shape * origin_shape
            out_shape = out_shape.flip(1).tile([1, 2]).unsqueeze(1)
        elif self.bbox_decode_type == 'origin':
            out_shape = origin_shape.flip(1).tile([1, 2]).unsqueeze(1)
        else:
            raise Exception(
                f'Wrong `bbox_decode_type`: {self.bbox_decode_type}.')
        bbox_pred *= out_shape

        scores = F.sigmoid(logits) if self.use_focal_loss else F.softmax(
            logits)[:, :, :-1]

        if not self.use_focal_loss:
            scores, labels = scores.max(-1), scores.argmax(-1)
            if scores.shape[1] > self.num_top_queries:
                scores, index = paddle.topk(
                    scores, self.num_top_queries, axis=-1)
                batch_ind = paddle.arange(
                    end=scores.shape[0]).unsqueeze(-1).tile(
                        [1, self.num_top_queries])
                index = paddle.stack([batch_ind, index], axis=-1)
                labels = paddle.gather_nd(labels, index)
                bbox_pred = paddle.gather_nd(bbox_pred, index)
        else:
            scores, index = paddle.topk(
                scores.flatten(1), self.num_top_queries, axis=-1)
            labels = index % self.num_classes
            index = index // self.num_classes
            batch_ind = paddle.arange(end=scores.shape[0]).unsqueeze(-1).tile(
                [1, self.num_top_queries])
            index = paddle.stack([batch_ind, index], axis=-1)
            bbox_pred = paddle.gather_nd(bbox_pred, index)

        mask_pred = None
        if self.with_mask:
            assert masks is not None
            masks = F.interpolate(
                masks, scale_factor=4, mode="bilinear", align_corners=False)
            # TODO: Support prediction with bs>1.
            # remove padding for input image
            h, w = im_shape.astype('int32')[0]
            masks = masks[..., :h, :w]
            # get pred_mask in the original resolution.
            img_h = img_h[0].astype('int32')
            img_w = img_w[0].astype('int32')
            masks = F.interpolate(
                masks,
                size=(img_h, img_w),
                mode="bilinear",
                align_corners=False)
            mask_pred, scores = self._mask_postprocess(masks, scores, index)

        bbox_pred = paddle.concat(
            [
                labels.unsqueeze(-1).astype('float32'), scores.unsqueeze(-1),
                bbox_pred
            ],
            axis=-1)
        bbox_num = paddle.to_tensor(
            self.num_top_queries, dtype='int32').tile([bbox_pred.shape[0]])
        bbox_pred = bbox_pred.reshape([-1, 6])
        return bbox_pred, bbox_num, mask_pred


@register
class MaskPostProcess(object):
    __shared__ = ['export_onnx', 'assign_on_cpu']
    """
    refer to:
    https://github.com/facebookresearch/detectron2/layers/mask_ops.py

    Get Mask output according to the output from model
    """

    def __init__(self,
                 binary_thresh=0.5,
                 export_onnx=False,
                 assign_on_cpu=False):
        super(MaskPostProcess, self).__init__()
        self.binary_thresh = binary_thresh
        self.export_onnx = export_onnx
        self.assign_on_cpu = assign_on_cpu

    def __call__(self, mask_out, bboxes, bbox_num, origin_shape):
        """
        Decode the mask_out and paste the mask to the origin image.

        Args:
            mask_out (Tensor): mask_head output with shape [N, 28, 28].
            bbox_pred (Tensor): The output bboxes with shape [N, 6] after decode
                and NMS, including labels, scores and bboxes.
            bbox_num (Tensor): The number of prediction boxes of each batch with
                shape [1], and is N.
            origin_shape (Tensor): The origin shape of the input image, the tensor
                shape is [N, 2], and each row is [h, w].
        Returns:
            pred_result (Tensor): The final prediction mask results with shape
                [N, h, w] in binary mask style.
        """
        num_mask = mask_out.shape[0]
        origin_shape = paddle.cast(origin_shape, 'int32')
        device = paddle.device.get_device()

        if self.export_onnx:
            h, w = origin_shape[0][0], origin_shape[0][1]
            mask_onnx = paste_mask(mask_out[:, None, :, :], bboxes[:, 2:], h, w,
                                   self.assign_on_cpu)
            mask_onnx = mask_onnx >= self.binary_thresh
            pred_result = paddle.cast(mask_onnx, 'int32')

        else:
            max_h = paddle.max(origin_shape[:, 0])
            max_w = paddle.max(origin_shape[:, 1])
            pred_result = paddle.zeros(
                [num_mask, max_h, max_w], dtype='int32') - 1

            id_start = 0
            for i in range(paddle.shape(bbox_num)[0]):
                bboxes_i = bboxes[id_start:id_start + bbox_num[i], :]
                mask_out_i = mask_out[id_start:id_start + bbox_num[i], :, :]
                im_h = origin_shape[i, 0]
                im_w = origin_shape[i, 1]
                pred_mask = paste_mask(mask_out_i[:, None, :, :],
                                       bboxes_i[:, 2:], im_h, im_w,
                                       self.assign_on_cpu)
                pred_mask = paddle.cast(pred_mask >= self.binary_thresh,
                                        'int32')
                pred_result[id_start:id_start + bbox_num[i], :im_h, :
                            im_w] = pred_mask
                id_start += bbox_num[i]
        if self.assign_on_cpu:
            paddle.set_device(device)

        return pred_result


def paste_mask(masks, boxes, im_h, im_w, assign_on_cpu=False):
    """
    Paste the mask prediction to the original image.
    """
    x0_int, y0_int = 0, 0
    x1_int, y1_int = im_w, im_h
    x0, y0, x1, y1 = paddle.split(boxes, 4, axis=1)
    N = masks.shape[0]
    img_y = paddle.arange(y0_int, y1_int) + 0.5
    img_x = paddle.arange(x0_int, x1_int) + 0.5

    img_y = (img_y - y0) / (y1 - y0) * 2 - 1
    img_x = (img_x - x0) / (x1 - x0) * 2 - 1
    # img_x, img_y have shapes (N, w), (N, h)

    if assign_on_cpu:
        paddle.set_device('cpu')
    gx = img_x[:, None, :].expand(
        [N, paddle.shape(img_y)[1], paddle.shape(img_x)[1]])
    gy = img_y[:, :, None].expand(
        [N, paddle.shape(img_y)[1], paddle.shape(img_x)[1]])
    grid = paddle.stack([gx, gy], axis=3)
    img_masks = F.grid_sample(masks, grid, align_corners=False)
    return img_masks[:, 0]


def multiclass_nms(bboxs, num_classes, match_threshold=0.6, match_metric='iou'):
    final_boxes = []
    for c in range(num_classes):
        idxs = bboxs[:, 0] == c
        if np.count_nonzero(idxs) == 0: continue
        r = nms(bboxs[idxs, 1:], match_threshold, match_metric)
        final_boxes.append(np.concatenate([np.full((r.shape[0], 1), c), r], 1))
    return final_boxes


def nms(dets, match_threshold=0.6, match_metric='iou'):
    """ Apply NMS to avoid detecting too many overlapping bounding boxes.
        Args:
            dets: shape [N, 5], [score, x1, y1, x2, y2]
            match_metric: 'iou' or 'ios'
            match_threshold: overlap thresh for match metric.
    """
    if dets.shape[0] == 0:
        return dets[[], :]
    scores = dets[:, 0]
    x1 = dets[:, 1]
    y1 = dets[:, 2]
    x2 = dets[:, 3]
    y2 = dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]

    ndets = dets.shape[0]
    suppressed = np.zeros((ndets), dtype=np.int)

    for _i in range(ndets):
        i = order[_i]
        if suppressed[i] == 1:
            continue
        ix1 = x1[i]
        iy1 = y1[i]
        ix2 = x2[i]
        iy2 = y2[i]
        iarea = areas[i]
        for _j in range(_i + 1, ndets):
            j = order[_j]
            if suppressed[j] == 1:
                continue
            xx1 = max(ix1, x1[j])
            yy1 = max(iy1, y1[j])
            xx2 = min(ix2, x2[j])
            yy2 = min(iy2, y2[j])
            w = max(0.0, xx2 - xx1 + 1)
            h = max(0.0, yy2 - yy1 + 1)
            inter = w * h
            if match_metric == 'iou':
                union = iarea + areas[j] - inter
                match_value = inter / union
            elif match_metric == 'ios':
                smaller = min(iarea, areas[j])
                match_value = inter / smaller
            else:
                raise ValueError()
            if match_value >= match_threshold:
                suppressed[j] = 1
    keep = np.where(suppressed == 0)[0]
    dets = dets[keep, :]
    return dets
