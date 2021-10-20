# To add a new cell, type '# %%'
# To add a new markdown cell, type '# %% [markdown]'
# %%
from IPython import get_ipython

# %% [markdown]
# # Mask R-CNN - Squirrel Monkey Segmentation

# %%
# Gets rid of a HOST of deprecation warnings for Matterport 
import warnings
warnings.filterwarnings("ignore")

import tensorflow as tf 

# and Tensorflow
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)


# %%
import os
import sys
import random
import numpy as np
import matplotlib.pyplot as plt
import skimage
from termcolor import colored

# Root directory of the project
ROOT_DIR = os.path.abspath("./Mask_RCNN")

# Import Mask RCNN
sys.path.append(ROOT_DIR)  # To find local version of the library
from mrcnn.config import Config
from mrcnn import utils
import mrcnn.model as modellib
from mrcnn import visualize
from mrcnn.model import log

# get_ipython().run_line_magic('matplotlib', 'inline')

# Directory to save logs and trained model
MODEL_DIR = os.path.join(ROOT_DIR, "logs")

# Local path to trained weights file
COCO_MODEL_PATH = os.path.join(ROOT_DIR, "mask_rcnn_coco.h5")
# COCO_MODEL_PATH = "C:\\Users\\addis\\Documents\\mask_rcnn_coco.h5"
# Download COCO trained weights from Releases if needed
if not os.path.exists(COCO_MODEL_PATH):
    utils.download_trained_weights(COCO_MODEL_PATH)


# %%
# Check tf version
print(tf.__version__)
print(tf.test.is_gpu_available())

# %% [markdown]
# ## Configurations
# 

# %%
class MonkeysConfig(Config):
    #################### BASE CONFIGURATION ####################
    NAME = "monkeys"

    # Train on 1 GPU, with batch size 1
    GPU_COUNT = 1
    BATCH_SIZE = 1

    # Number of classes (including background)
    NUM_CLASSES = 1 + 1  # background + 1 monkey
    DETECTION_MAX_INSTANCES = 1  # we're only looking for the most prominent individual in each image

    # Reduce training ROIs per image because the images are small and have
    # few objects. Aim to allow ROI sampling to pick 33% positive ROIs.
    TRAIN_ROIS_PER_IMAGE = 32

    # RPN ANCHOR SCALES left as default (32, 64, 128, 256, 512), in line with the FaterRCNN paper
    RPN_ANCHOR_SCALES = (8, 16, 32, 64, 128)  # anchor side in pixels
    
    # (the number of batch iterations before a training epoch is considered finished). As we want to train on the full dataset, it's equal to num_samples/batch_size
    # STEPS_PER_EPOCH = 100

    # VALIDATION_STEPS is similiar to STEPS_PER_EPOCH



def get_config(learning_rate, image_dim, detection_nms_threshold, detection_min_confidence):
    # config used for both training and inference
    config = MonkeysConfig()

    #################### HYPERPARAMETERS TO TUNE ####################
    config.IMAGE_MAX_DIM = image_dim
    config.IMAGE_MIN_DIM = image_dim
    config.LEARNING_RATE = learning_rate
    config.DETECTION_NMS_THRESHOLD = detection_nms_threshold
    config.DETECTION_MIN_CONFIDENCE = detection_min_confidence
    config.display()

    return config

# %% [markdown]
# ## Dataset
# 
# Handles loading images and masks for the custom dataset
# 

# %%
import json
MONKEY_CLASS_ID_STR = "monkey"


class MonkeysDataset(utils.Dataset):
    def load_monkeys(self, dataset_dir, subset):

        # Add classes
        self.add_class(MONKEY_CLASS_ID_STR, 1, MONKEY_CLASS_ID_STR)

        num_images_added = 0
       # Load annotations
        # VGG Image Annotator (up to version 1.6) saves each image in the form:
        # { 'filename': '28503151_5b5b7ec140_b.jpg',
        #   'regions': {
        #       '0': {
        #           'region_attributes': {},
        #           'shape_attributes': {
        #               'all_points_x': [...],
        #               'all_points_y': [...],
        #               'name': 'polygon'}},
        #       ... more regions ...
        #   },
        #   'size': 100202
        # }
        # We mostly care about the x and y coordinates of each region
        # Note: In VIA 2.0, regions was changed from a dict to a list.
        annotations = json.load(
            open(os.path.join(dataset_dir, "via_region_data.json")))
        annotations = list(annotations.values())  # don't need the dict keys

        # The VIA tool saves images in the JSON even if they don't have any
        # annotations. Skip unannotated images.
        annotations = [a for a in annotations if a['regions']]

        # Add images
        for a in annotations:
            if a['filename'] in subset:
                # Get the x, y coordinaets of points of the polygons that make up
                # the outline of each object instance. These are stores in the
                # shape_attributes (see json format above)
                # The if condition is needed to support VIA versions 1.x and 2.x.
                if type(a['regions']) is dict:
                    polygons = [r['shape_attributes']
                                for r in a['regions'].values()]
                else:
                    polygons = [r['shape_attributes'] for r in a['regions']]

                # load_mask() needs the image size to convert polygons to masks.
                # Unfortunately, VIA doesn't include it in JSON, so we must read
                # the image. This is only managable since the dataset is tiny.
                image_path = os.path.join(dataset_dir, "images", a['filename'])
                image = skimage.io.imread(image_path)
                height, width = image.shape[:2]

                self.add_image(
                    MONKEY_CLASS_ID_STR,
                    image_id=a['filename'],  # use file name as a unique image id
                    path=image_path,
                    width=width, height=height,
                    polygons=polygons)

                num_images_added += 1
                print(colored(f"Loading images {num_images_added}/{len(annotations)}"), end='\r')

    def load_mask(self, image_id):
        """Generate instance masks for an image.
       Returns:
        masks: A bool array of shape [height, width, instance count] with
            one mask per instance.
        class_ids: a 1D array of class IDs of the instance masks.
        """
        # If not a monkey dataset image, delegate to parent class.
        image_info = self.image_info[image_id]
        if image_info["source"] != MONKEY_CLASS_ID_STR:
            return super(self.__class__, self).load_mask(image_id)

        # Convert polygons to a bitmap mask of shape
        # [height, width, instance_count]
        info = self.image_info[image_id]
        mask = np.zeros([info["height"], info["width"], len(info["polygons"])],
                        dtype=np.uint8)
        for i, p in enumerate(info["polygons"]):
            # Get indexes of pixels inside the polygon and set them to 1
            rr, cc = skimage.draw.polygon(p['all_points_y'], p['all_points_x'])
            mask[rr, cc, i] = 1

        # Return mask, and array of class IDs of each instance. Since we have
        # one class ID only, we return an array of 1s
        return mask.astype(np.bool), np.ones([mask.shape[-1]], dtype=np.int32)

    def image_reference(self, image_id):
        """Return the path of the image."""
        info = self.image_info[image_id]
        if info["source"] == MONKEY_CLASS_ID_STR:
            return info["path"]
        else:
            super(self.__class__, self).image_reference(image_id)

# %% [markdown]
# # Loading Dataset
# 
# Ensure the dataset is in the following form:
# 
# dirName  
# └── train  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── a.jpg  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── b.jpg  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── c.jpg  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└── via_region_data.json  
# └── val  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── c.jpg  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── d.jpg  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;├── e.jpg  
# &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;└── via_regon_data.json  

# %%
import glob
from pathlib import Path
DATASET_DIR = Path("F:/Adam/Pictures/AucklandZooImages/cv set (240)")

path = (DATASET_DIR / "images" / "*.*")

X = [os.path.basename(x) for x in glob.glob(str(path))]
print(X[:3])

# %% [markdown]
# ## K-Fold Validation

# %%
def prepare_model_train_dataset(X_train_fold):
    print("Preparing Dataset")
    # Annotated using: https://www.robots.ox.ac.uk/~vgg/software/via/
    dataset = MonkeysDataset()
    dataset.load_monkeys(DATASET_DIR, subset=X_train_fold)
    dataset.prepare()

    return dataset


# %%
def prepare_train_model(model_configuration):
    print("Preparing training model...")

    # Create model in training mode
    model = modellib.MaskRCNN(mode="training", config=model_configuration,
                              model_dir=MODEL_DIR)

    model.load_weights(COCO_MODEL_PATH, by_name=True,
                       exclude=["mrcnn_class_logits", "mrcnn_bbox_fc",
                                "mrcnn_bbox", "mrcnn_mask"])

    return model


# %%
def train_model(dataset_train, dataset_val, train_config, training_epochs, fine_tune_epochs):

    model = prepare_train_model(train_config)

    print("Training model head layers")
    # Train the head branches
    # Passing layers="heads" freezes all layers except the head
    # layers. You can also pass a regular expression to select
    # which layers to train by name pattern.

    model.train(dataset_train, dataset_val,
                learning_rate=train_config.LEARNING_RATE,
                epochs=training_epochs,
                layers='heads')

    print("Fine tuning whole model")

    # Fine tune all layers
    # Passing layers="all" trains all layers. You can also
    # pass a regular expression to select which layers to
    # train by name pattern.
    model.train(dataset_train, dataset_val,
                learning_rate=train_config.LEARNING_RATE / 10,  # TODO determine why this is / 10
                epochs=training_epochs+fine_tune_epochs,
                layers="all")


# %%
def get_inference_model(inference_config):
    print("Getting inference model")
    # Recreate the model in inference mode
    model = modellib.MaskRCNN(mode="inference",
                              config=inference_config,
                              model_dir=MODEL_DIR)

    # Get path to saved weights
    # Either set a specific path or find last trained weights
    # model_path = os.path.join(ROOT_DIR, ".h5 file name here")
    model_path = model.find_last()

    # Load trained weights
    print("Loading weights from ", model_path)
    model.load_weights(model_path, by_name=True)

    return model


def evaluate_model(dataset_val, inference_config):
    print("Evaluating model...")
    # **NEED TO CHANGE**

    # Compute VOC-Style mAP @ IoU=0.5
    # Running on 10 images. Increase for better accuracy.
    # image_ids = np.random.choice(dataset_val.image_ids, 10)

    image_ids = dataset_val.image_ids

    model = get_inference_model(inference_config)

    APs = []
    for image_id in image_ids:
        # Load image and ground truth data
        image, image_meta, gt_class_id, gt_bbox, gt_mask =            modellib.load_image_gt(dataset_val, inference_config,
                                   image_id, use_mini_mask=False)
        molded_images = np.expand_dims(modellib.mold_image(image, inference_config), 0)
        # Run object detection
        results = model.detect([image], verbose=0)
        r = results[0]
        # Compute AP
        AP, precisions, recalls, overlaps =            utils.compute_ap(gt_bbox, gt_class_id, gt_mask,
                             r["rois"], r["class_ids"], r["scores"], r['masks'])
        APs.append(AP)

    print("Evaluating model done")
    return np.mean(APs)


# %%
from sklearn.model_selection import train_test_split


def kfold_model(n_splits, X_train, model_config, train_epochs, fine_tune_epochs):

    kf = KFold(n_splits=n_splits, random_state=SEED, shuffle=True)

    mAPs = []

    for train_index, test_index in kf.split(X_train):
        X_train = np.array(X_train)
        X_train_fold, X_test_fold = X_train[train_index], X_train[test_index]

        # Split our training data further into a train and validation set that will be used during *Training*
        X_train_train_sub, X_train_val_sub = train_test_split(X_train_fold, test_size=0.1, random_state=SEED)

        # Train and validation sets used during model training
        dataset_train = prepare_model_train_dataset(X_train_train_sub)
        dataset_val = prepare_model_train_dataset(X_train_val_sub)

        # Test set used to evaluate model performance *Testing*
        dataset_test = prepare_model_train_dataset(X_test_fold)

        train_model(dataset_train, dataset_val, model_config, train_epochs, fine_tune_epochs)

        # Mean Average Precision for the trained model
        mAP = evaluate_model(dataset_test, model_config)

        mAPs.append(mAP)

    # Averaged mAP accross the k folds
    averaged_mAPs = np.mean(mAPs)

    return averaged_mAPs


# %%
from itertools import product
from sklearn.model_selection import KFold


def test_hyperparameters(num_folds, X_train, train_epochs, fine_tune_epochs):
    learning_rate_search_space = [0.0001]  # [1e-5, 1e-4, 1e-3, 1e-2, 1e-1]
    image_dim_search_space = [128, 256, 512, 1024]
    detection_nms_search_space = [0.2, 0.3, 0.4]
    detection_min_confidence_search_space = [0.7, 0.8, 0.9, 0.95]

    num_hyperparameters = 4

    search_permutations = list(product(learning_rate_search_space, image_dim_search_space, detection_nms_search_space, detection_min_confidence_search_space))

    results = np.zeros((len(search_permutations), num_hyperparameters + 1))

    for i, combination in enumerate(search_permutations):
        learning_rate, image_dim, detection_nms_threshold, detection_min_confidence = combination

        print(f"Evaluating variation {i+1}/{len(search_permutations)}")

        # Model configurations, with hyperparameters
        model_config = get_config(learning_rate, image_dim, detection_nms_threshold, detection_min_confidence)
        averaged_mAPs = kfold_model(num_folds, X_train, model_config, train_epochs, fine_tune_epochs)

        results[i, :] = learning_rate, image_dim, detection_nms_threshold, detection_min_confidence, averaged_mAPs

    return results


# %%

SEED = 123

X_train, X_val = train_test_split(X, test_size=0.2, random_state=SEED)

print(f"X_train length: {len(X_train)}")
print(f"Reservered X_val length: {len(X_val)}")


# %%
num_folds = 3
num_train_epochs = 5
num_fine_tune_epochs = 5

results = test_hyperparameters(num_folds, X_train, num_train_epochs, num_fine_tune_epochs)


# %%


