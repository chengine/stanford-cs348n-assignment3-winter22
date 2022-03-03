"""
    the box-represented shape VAE/AE model (no horizontal edges)
"""

import os
import sys
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from cd.chamfer import chamfer_distance
from data import Tree
from utils import load_pts, transform_pc_batch, get_surface_reweighting_batch
from scipy.optimize import linear_sum_assignment


class Sampler(nn.Module):

    def __init__(self, feature_size, hidden_size, probabilistic=True):
        super(Sampler, self).__init__()
        self.probabilistic = probabilistic

        self.mlp1 = nn.Linear(feature_size, hidden_size)
        self.mlp2mu = nn.Linear(hidden_size, feature_size)
        self.mlp2var = nn.Linear(hidden_size, feature_size)

    def forward(self, x):
        encode = torch.relu(self.mlp1(x))
        mu = self.mlp2mu(encode)

        if self.probabilistic:
            logvar = self.mlp2var(encode)
            std = logvar.mul(0.5).exp_()
            eps = torch.randn_like(std)

            kld = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
            return torch.cat([eps.mul(std).add_(mu), kld], 1)
        else:
            return mu


class BoxEncoder(nn.Module):

    def __init__(self, feature_size):
        super(BoxEncoder, self).__init__()
        self.encoder = nn.Linear(10, feature_size)

    def forward(self, box_input):
        box_vector = torch.relu(self.encoder(box_input))
        return box_vector


class SymmetricChildEncoder(nn.Module):

    def __init__(self, feature_size, hidden_size):
        super(SymmetricChildEncoder, self).__init__()

        self.mlp1 = nn.Linear(feature_size + Tree.num_sem, hidden_size)
        self.mlp2 = nn.Linear(hidden_size, feature_size)

    def forward(self, child_feats, child_exists):
        batch_size = child_feats.shape[0]
        max_childs = child_feats.shape[1]
        feat_size = child_feats.shape[2]

        # STUDENT CODE START
        # use the mlp1 linear layer to extract per-child features
        feat = torch.relu(self.mlp1(child_feats.view(batch_size*max_childs, feat_size))).view(batch_size, max_childs, -1)

        # zero non-existent children
        x = feat.view(batch_size*max_childs, -1)*child_exists

        # perform max-pooling over children nodes
        x = torch.max(x.view(batch_size,max_childs, -1), dim=1)[0]

        # use the mlp2 linear layer to summarize a parent node feature
        parent_feat = torch.relu(self.mlp2(x.view(batch_size, -1)))

        # STUDENT CODE END

        return parent_feat


class RecursiveEncoder(nn.Module):

    def __init__(self, config, variational=False, probabilistic=True):
        super(RecursiveEncoder, self).__init__()
        self.conf = config

        self.box_encoder = BoxEncoder(feature_size=config.feature_size)
        self.child_encoder = SymmetricChildEncoder(
                feature_size=config.feature_size, 
                hidden_size=config.hidden_size)

        if variational:
            self.sample_encoder = Sampler(feature_size=config.feature_size, \
                    hidden_size=config.hidden_size, probabilistic=probabilistic)
    
    def encode_node(self, node):
        if node.is_leaf:
            return self.box_encoder(node.get_box_quat())
        else:
            # get features of all children
            child_feats = []
            for child in node.children:
                cur_child_feat = torch.cat([self.encode_node(child), child.get_semantic_one_hot()], dim=1)
                child_feats.append(cur_child_feat.unsqueeze(dim=1))
            child_feats = torch.cat(child_feats, dim=1)

            if child_feats.shape[1] > self.conf.max_child_num:
                raise ValueError('Node has too many children.')

            # pad with zeros
            if child_feats.shape[1] < self.conf.max_child_num:
                padding = child_feats.new_zeros(child_feats.shape[0], \
                        self.conf.max_child_num-child_feats.shape[1], child_feats.shape[2])
                child_feats = torch.cat([child_feats, padding], dim=1)

            # 1 if the child exists, 0 if it is padded
            child_exists = child_feats.new_zeros(child_feats.shape[0], self.conf.max_child_num, 1)
            child_exists[:, :len(node.children), :] = 1

            # get feature of current node (parent of the children)
            return self.child_encoder(child_feats, child_exists)

    def encode_structure(self, obj):
        root_latent = self.encode_node(obj.root)
        return self.sample_encoder(root_latent)


class LeafClassifier(nn.Module):

    def __init__(self, feature_size, hidden_size):
        super(LeafClassifier, self).__init__()
        self.mlp1 = nn.Linear(feature_size, hidden_size)
        self.mlp2 = nn.Linear(hidden_size, 1)

    def forward(self, input_feature):
        output = torch.relu(self.mlp1(input_feature))
        output = self.mlp2(output)
        return output


class SampleDecoder(nn.Module):

    def __init__(self, feature_size, hidden_size):
        super(SampleDecoder, self).__init__()
        self.mlp1 = nn.Linear(feature_size, hidden_size)
        self.mlp2 = nn.Linear(hidden_size, feature_size)

    def forward(self, input_feature):
        output = torch.relu(self.mlp1(input_feature))
        output = torch.relu(self.mlp2(output))
        return output


class BoxDecoder(nn.Module):

    def __init__(self, feature_size, hidden_size):
        super(BoxDecoder, self).__init__()
        self.mlp = nn.Linear(feature_size, hidden_size)
        self.center = nn.Linear(hidden_size, 3)
        self.size = nn.Linear(hidden_size, 3)
        self.quat = nn.Linear(hidden_size, 4)

    def forward(self, parent_feature):
        feat = torch.relu(self.mlp(parent_feature))
        center = torch.tanh(self.center(feat))
        size = torch.sigmoid(self.size(feat)) * 2
        quat_bias = feat.new_tensor([[1.0, 0.0, 0.0, 0.0]])
        quat = self.quat(feat).add(quat_bias)
        quat = quat / (1e-12 + quat.pow(2).sum(dim=1).unsqueeze(dim=1).sqrt())
        vector = torch.cat([center, size, quat], dim=1)
        return vector


class ConcatChildDecoder(nn.Module):

    def __init__(self, feature_size, hidden_size, max_child_num):
        super(ConcatChildDecoder, self).__init__()

        self.max_child_num = max_child_num
        self.hidden_size = hidden_size

        self.mlp_parent = nn.Linear(feature_size, hidden_size*max_child_num)
        self.mlp_exists = nn.Linear(hidden_size, 1)
        self.mlp_sem = nn.Linear(hidden_size, Tree.num_sem)
        self.mlp_child = nn.Linear(hidden_size, feature_size)

    def forward(self, parent_feature):
        batch_size = parent_feature.shape[0]
        feat_size = parent_feature.shape[1]
        
        # STUDENT CODE START
        # use the mlp_parent linear layer to get the children node features
        par_feats = torch.relu(self.mlp_parent(parent_feature))
        child_feats = par_feats.view(batch_size, self.max_child_num, self.hidden_size)

        # use the mlp_exists linear layer to predict children node existence (output logits, i.e. no sigmoid)
        child_exists_logits = self.mlp_exists(child_feats.view(batch_size*self.max_child_num, -1)).view(batch_size, self.max_child_num, -1)
        # use the mlp_sem linear layer to predict children node semantics (output logits, i.e. no sigmoid)
        child_sem_logits = self.mlp_sem(child_feats.view(batch_size*self.max_child_num, -1)).view(batch_size, self.max_child_num, -1)
        # use the mlp_child linear layer to further evolve the children node features
        child_feats = torch.relu(self.mlp_child(child_feats.view(batch_size*self.max_child_num, -1))).view(batch_size, self.max_child_num, -1)
        # STUDENT CODE END

        return child_feats, child_sem_logits, child_exists_logits


class RecursiveDecoder(nn.Module):
    
    def __init__(self, config):
        super(RecursiveDecoder, self).__init__()

        self.conf = config

        self.box_decoder = BoxDecoder(config.feature_size, config.hidden_size)

        self.child_decoder = ConcatChildDecoder(
                feature_size=config.feature_size, 
                hidden_size=config.hidden_size, 
                max_child_num=config.max_child_num)

        self.sample_decoder = SampleDecoder(config.feature_size, config.hidden_size)
        self.leaf_classifier = LeafClassifier(config.feature_size, config.hidden_size)

        self.bceLoss = nn.BCEWithLogitsLoss(reduction='none')
        self.semCELoss = nn.CrossEntropyLoss(reduction='none')

        self.register_buffer('unit_cube', torch.from_numpy(load_pts('cube.pts')))

    def boxLossEstimator(self, box_feature, gt_box_feature):
        pred_box_pc = transform_pc_batch(self.unit_cube, box_feature)
        with torch.no_grad():
            pred_reweight = get_surface_reweighting_batch(box_feature[:, 3:6], self.unit_cube.size(0))
        gt_box_pc = transform_pc_batch(self.unit_cube, gt_box_feature)
        with torch.no_grad():
            gt_reweight = get_surface_reweighting_batch(gt_box_feature[:, 3:6], self.unit_cube.size(0))
        dist1, dist2 = chamfer_distance(gt_box_pc, pred_box_pc, transpose=False)
        loss1 = (dist1 * gt_reweight).sum(dim=1) / (gt_reweight.sum(dim=1) + 1e-12)
        loss2 = (dist2 * pred_reweight).sum(dim=1) / (pred_reweight.sum(dim=1) + 1e-12)
        loss = (loss1 + loss2) / 2
        return loss
    
    def isLeafLossEstimator(self, is_leaf_logit, gt_is_leaf):
        return self.bceLoss(is_leaf_logit, gt_is_leaf).view(-1)

    # decode a root code into a tree structure
    def decode_structure(self, z, max_depth):
        root_latent = self.sample_decoder(z)
        root = self.decode_node(root_latent, max_depth, Tree.root_sem)
        obj = Tree(root=root)
        return obj

    # decode a part node (inference only)
    def decode_node(self, node_latent, max_depth, full_label, is_leaf=False):
        if node_latent.shape[0] != 1:
            raise ValueError('Node decoding does not support batch_size > 1.')

        is_leaf_logit = self.leaf_classifier(node_latent)
        node_is_leaf = is_leaf_logit.item() > 0

        # use maximum depth to avoid potential infinite recursion
        if max_depth < 1:
            is_leaf = True

        # decode the current part box
        box = self.box_decoder(node_latent)

        if node_is_leaf or is_leaf:
            ret = Tree.Node(is_leaf=True, \
                    full_label=full_label, label=full_label.split('/')[-1])
            ret.set_from_box_quat(box.view(-1))
            return ret
        else:
            child_feats, child_sem_logits, child_exists_logit = \
                    self.child_decoder(node_latent)
            
            child_sem_logits = child_sem_logits.cpu().numpy().squeeze()

            # children
            child_nodes = []
            for ci in range(child_feats.shape[1]):
                if torch.sigmoid(child_exists_logit[:, ci, :]).item() > 0.5:
                    idx = np.argmax(child_sem_logits[ci, Tree.part_name2cids[full_label]])
                    idx = Tree.part_name2cids[full_label][idx]
                    child_full_label = Tree.part_id2name[idx]
                    child_nodes.append(self.decode_node(\
                            child_feats[:, ci, :], max_depth-1, child_full_label, \
                            is_leaf=(child_full_label not in Tree.part_non_leaf_sem_names)))

            ret = Tree.Node(is_leaf=False, children=child_nodes, \
                    full_label=full_label, label=full_label.split('/')[-1])
            ret.set_from_box_quat(box.view(-1))
            return ret
 
    # use gt structure, compute the reconstruction losses
    def structure_recon_loss(self, z, gt_tree):
        root_latent = self.sample_decoder(z)
        losses = self.node_recon_loss(root_latent, gt_tree.root)
        return losses

    # use gt structure, compute the reconstruction losses (used during training)
    def node_recon_loss(self, node_latent, gt_node):
        if gt_node.is_leaf:
            box = self.box_decoder(node_latent)
            box_loss = self.boxLossEstimator(box, gt_node.get_box_quat().view(1, -1))
            is_leaf_logit = self.leaf_classifier(node_latent)
            is_leaf_loss = self.isLeafLossEstimator(is_leaf_logit, is_leaf_logit.new_tensor(gt_node.is_leaf).view(1, -1))
            return {'box': box_loss, 'leaf': is_leaf_loss, \
                    'exists': torch.zeros_like(box_loss), 'semantic': torch.zeros_like(box_loss)}
        else:
            child_feats, child_sem_logits, child_exists_logits = \
                    self.child_decoder(node_latent)
            
            # generate box prediction for each child
            feature_len = node_latent.size(1)
            # |PRED_BOXES| x 10
            child_pred_boxes = self.box_decoder(child_feats.view(-1, feature_len))
            num_child_parts = child_pred_boxes.size(0)
            
            # perform hungarian matching between pred boxes and gt boxes
            with torch.no_grad():
                # |GT_BOXES| x 10
                child_gt_boxes = torch.cat([child_node.get_box_quat() for child_node in gt_node.children], dim=0)
                num_gt = child_gt_boxes.size(0)

                # STUDENT CODE START
                # given the predicted boxes child_pred_boxes and the GT boxes child_gt_boxes
                # we need to match every GT box to one predicted box
                # the function scipy.optimize.linear_sum_assignment can be used
                # https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html
                #
                # as the input, we need to feed in a distance matrix
                # compute the distance matrix of size |GT_BOXES| x |PRED_BOXES|
                # use the self.boxLossEstimator to estimate distances between the GT and PRED boxes
                #   -  the input sizes to the function is B x 10, B x 10
                #   - the output size is B
                child_pred_boxes_repeat = child_pred_boxes[None,...].repeat(num_gt, 1, 1)
                child_gt_boxes_repeat = child_gt_boxes[:, None,...].repeat(1, num_child_parts, 1)
                dist_mat = self.boxLossEstimator(child_pred_boxes_repeat.view(num_gt*num_child_parts, -1), child_gt_boxes_repeat.view(num_gt*num_child_parts, -1))
                dist_mat = dist_mat.reshape(num_gt, num_child_parts)
                #dist_mat = torch.transpose(dist_mat.view(num_child_parts, num_gt), 0, 1)
                # STUDENT CODE END

                # returned matched_gt_idx contains a list of row indices (the GT box IDs) that are matched
                # returned matched_pred_idx contains a list of column indices (the PRED box IDS) that are matched
                matched_gt_idx, matched_pred_idx = linear_sum_assignment(dist_mat.to('cpu').numpy())
                matched_gt_idx = list(matched_gt_idx)
                matched_pred_idx = list(matched_pred_idx)

                if len(matched_gt_idx) > 0:
                    matched_gt_idx, matched_pred_idx = zip(*sorted(zip(matched_gt_idx, matched_pred_idx)))
                    matched_gt_idx = list(matched_gt_idx)
                    matched_pred_idx = list(matched_pred_idx)

            # train the current node to be non-leaf
            is_leaf_logit = self.leaf_classifier(node_latent)
            is_leaf_loss = self.isLeafLossEstimator(is_leaf_logit, is_leaf_logit.new_tensor(gt_node.is_leaf).view(1, -1))

            # train the current node box to gt
            box = self.box_decoder(node_latent)
            box_loss = self.boxLossEstimator(box, gt_node.get_box_quat().view(1, -1))

            # gather information
            child_sem_gt_labels = []
            child_sem_pred_logits = []
            child_box_gt = []
            child_box_pred = []
            child_exists_gt = torch.zeros_like(child_exists_logits)
            for i in range(len(matched_gt_idx)):
                child_sem_gt_labels.append(gt_node.children[matched_gt_idx[i]].get_semantic_id())
                child_sem_pred_logits.append(child_sem_logits[0, matched_pred_idx[i], :].view(1, -1))
                child_box_gt.append(gt_node.children[matched_gt_idx[i]].get_box_quat())
                child_box_pred.append(child_pred_boxes[matched_pred_idx[i], :].view(1, -1))
                child_exists_gt[:, matched_pred_idx[i], :] = 1
                
            # train semantic labels
            child_sem_pred_logits = torch.cat(child_sem_pred_logits, dim=0)
            child_sem_gt_labels = torch.tensor(child_sem_gt_labels, dtype=torch.int64, \
                    device=child_sem_pred_logits.device)
            semantic_loss = self.semCELoss(child_sem_pred_logits, child_sem_gt_labels)
            semantic_loss = semantic_loss.sum()

            # train unused boxes to zeros
            unmatched_boxes = []
            for i in range(num_child_parts):
                if i not in matched_pred_idx:
                    unmatched_boxes.append(child_pred_boxes[i, 3:6].view(1, -1))
            if len(unmatched_boxes) > 0:
                unmatched_boxes = torch.cat(unmatched_boxes, dim=0)
                unused_box_loss = unmatched_boxes.pow(2).sum() * 0.01
            else:
                unused_box_loss = 0.0

            # train exist scores
            child_exists_loss = F.binary_cross_entropy_with_logits(
                input=child_exists_logits, target=child_exists_gt, reduction='none')
            child_exists_loss = child_exists_loss.sum()
            
            # calculate children + aggregate losses
            for i in range(len(matched_gt_idx)):
                child_losses = self.node_recon_loss(\
                        child_feats[:, matched_pred_idx[i], :], gt_node.children[matched_gt_idx[i]])
                box_loss = box_loss + child_losses['box']
                is_leaf_loss = is_leaf_loss + child_losses['leaf']
                child_exists_loss = child_exists_loss + child_losses['exists']
                semantic_loss = semantic_loss + child_losses['semantic']

            return {'box': box_loss + unused_box_loss, 'leaf': is_leaf_loss,
                    'exists': child_exists_loss, 'semantic': semantic_loss}


