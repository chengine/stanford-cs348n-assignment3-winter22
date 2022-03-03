import os
import matplotlib
import matplotlib.pyplot as plt
from data import PartNetDataset
from vis_utils import draw_partnet_objects

# ground-truth data directory
root_dir = './structurenet_chair_dataset'

# load one data
obj = PartNetDataset.load_object(os.path.join(root_dir, '2233.json'))

# print the hierarchical structure
print('PartNet Hierarchy: (the number in bracket corresponds to PartNet part_id)')
print(obj)

draw_partnet_objects(objects=[obj], object_names=['GT'], 
                     figsize=(9, 5), leafs_only=True, 
                     sem_colors_filename='./part_colors_Chair.txt')