import os
from torch.utils.data import Dataset
import torch
import pandas as pd
from preprocessing import window_image
from tqdm import tqdm
from sklearn.model_selection import train_test_split
import nibabel
import pickle
import numpy as np
import csv
import pydicom
from torchvision.transforms.functional import rotate


class RSNAICHDataset(Dataset):
    def __init__(self, root_dir, filenames, labels, windows=None, transform=None):
        """
        Specific pytorch dataset designed for RSNA ICH dataset
        """
        self.train_dir = os.path.join(root_dir, 'stage_2_train')
        self.filenames = filenames
        self.labels = labels
        self.transform = transform
        self.windows = windows

    def __len__(self):
        return len(self.filenames)

    def __getitem__(self, item):
        if torch.is_tensor(item):
            item = item.tolist()

        image_path = os.path.join(self.train_dir, self.filenames[item])

        image, default_window_params = _read_image_2d(image_path)  # x, y
        window_center, window_width, window_intercept, window_slope = default_window_params
        label = torch.FloatTensor(self.labels[item])

        default_window = _get_image_windows(image, [(window_center, window_width)], window_intercept, window_slope)
        if self.windows is not None:
            image = torch.cat([default_window, _get_image_windows(image, self.windows, window_intercept, window_slope)])
        else:
            image = default_window

        if self.transform is not None:
            image = self.transform(image)

        return image, label


class PhysioNetICHDataset(Dataset):
    def __init__(self, root_dir, windows=None, transform=None):
        """
        Specific pytorch dataset designed for PhysioNet ICH dataset
        """
        self.scans_dir = os.path.join(root_dir, 'ct_scans')
        self.masks_dir = os.path.join(root_dir, 'masks')
        self.filenames = os.listdir(self.scans_dir)

        self.slices = []
        self.scans_num_slices = []
        self.masks = []
        self.labels = []
        self.transform = transform
        self.windows = windows

        self.labels_path = os.path.join(root_dir, 'hemorrhage_diagnosis_raw_ct.csv')
        self.read_dataset()

    def read_dataset(self):
        SUBTYPES = ["Epidural", "Intraparenchymal", "Intraventricular", "Subarachnoid", "Subdural", "No_Hemorrhage"]
        with open(self.labels_path, newline='') as labels_csv:
            reader = csv.DictReader(labels_csv)
            for row in reader:
                label = np.zeros(len(SUBTYPES))
                for i, subtype in enumerate(SUBTYPES):
                    label[i] = float(row[subtype])
                label[-1] = 1 - label[-1]  # Any hemorrhage = 1 - No Hemorrhage

                self.labels.append(label)
        self.labels = np.array(self.labels)

        k = 0
        pbar = tqdm(self.filenames, total=len(self.filenames))
        pbar.set_description("reading physionet dataset")
        for file in pbar:
            scan = _read_image_3d(os.path.join(self.scans_dir, file), do_rotate=True)
            mask = _read_image_3d(os.path.join(self.masks_dir, file), do_rotate=True)

            num_slices = scan.shape[-1]
            self.scans_num_slices.append(num_slices)

            for i in range(num_slices):
                self.slices.append(scan[:, :, i])
                self.masks.append(mask[:, :, i])
                k += 1

    def __len__(self):
        return len(self.slices)

    def __getitem__(self, item):
        if torch.is_tensor(item):
            item = item.tolist()

        image = self.slices[item]
        mask = self.masks[item]
        label = self.labels[item]

        if mask.max() > 0:  # change to range to 0-1
            mask = (mask - mask.min()) / (mask.max() - mask.min())

        default_window = _get_image_windows(image, [(40, 120)], 0, 1)
        if self.windows is not None:
            image = torch.cat([default_window, _get_image_windows(image, self.windows, 0, 1)])
        else:
            image = default_window

        if self.transform is not None:
            image = self.transform(image)
            mask = self.transform(mask.unsqueeze(0)).squeeze()

        return image, mask, label


def physio_collate_image_mask(batch):
    data = torch.stack([item[0] for item in batch])
    mask = torch.stack([item[1] for item in batch])

    return [data, mask]


def physio_collate_image_label(batch):
    data = torch.stack([item[0] for item in batch])
    target = torch.stack([item[2] for item in batch])

    return [data, target]


def rsna_collate_binary_label(batch):
    data = torch.stack([item[0] for item in batch])
    target = torch.stack([item[1] for item in batch])
    target = target[:, -1:]
    return [data, target]


def rsna_collate_subtypes_label(batch):
    data = torch.stack([item[0] for item in batch])
    target = torch.stack([item[1] for item in batch])
    target = target[:, :-1]
    return [data, target]


def rsna_train_valid_split(root_dir: str, validation_size=0.05, random_state=42, override=False):
    """
       a method that splits the 2D dicom dataset into train and validation set based on the number of slices containing hemorrhage
       we save the split into files for faster computation and further requirements
    """
    SUBTYPES = ["epidural", "intraparenchymal", "intraventricular", "subarachnoid", "subdural", "any"]
    train_file_split_path, validation_file_split_path = os.path.join(root_dir, 'train_file_split'), os.path.join(root_dir, 'validation_file_split')
    train_label_split_path, validation_label_split_path = os.path.join(root_dir, 'train_label_split'), os.path.join(root_dir, 'validation_label_split')
    if os.path.isfile(train_file_split_path) and os.path.isfile(validation_file_split_path) and os.path.isfile(train_label_split_path) and os.path.isfile(validation_label_split_path) and not override:
        with open(train_file_split_path, "rb") as tf, open(train_label_split_path, "rb") as tl, open(validation_file_split_path, "rb") as vf, open(validation_label_split_path, "rb") as vl:
            return pickle.load(tf), pickle.load(tl), pickle.load(vf), pickle.load(vl)

    labels_path = os.path.join(root_dir, 'stage_2_train.csv')
    train_path = os.path.join(root_dir, 'stage_2_train')

    total_filenames = os.listdir(train_path)
    corrupted_files = ['ID_6431af929.dcm']
    for corrupted_file in corrupted_files:
        if corrupted_file in total_filenames:
            total_filenames.remove(corrupted_file)

    labels_dict = {}
    with open(labels_path, newline='') as labels_csv:
        reader = csv.reader(labels_csv, delimiter=",")
        for i, row in enumerate(reader):
            if i == 0:
                continue
            labels_dict[row[0]] = float(row[1])

    labels = []
    pbar = tqdm(total_filenames, total=len(total_filenames))
    pbar.set_description("reading files and labels")
    for filename in pbar:
        labels.append([float(labels_dict[key]) for key in [filename.split('.')[0] + "_" + x for x in SUBTYPES]])

    labels = np.array(labels)

    train_filenames, validation_filenames, train_labels, validation_labels = train_test_split(total_filenames, labels, test_size=validation_size, random_state=random_state)
    with open(train_file_split_path, "wb") as tf, open(train_label_split_path, "wb") as tl, open(validation_file_split_path, "wb") as vf, open(validation_label_split_path, "wb") as vl:
        pickle.dump(train_filenames, tf), pickle.dump(train_labels, tl), pickle.dump(validation_filenames, vf), pickle.dump(validation_labels, vl)

    return train_filenames, train_labels, validation_filenames, validation_labels


def _get_image_windows(image, windows: [(int, int)], intercept, slope):
    window_images = []
    for window in windows:
        window_images.append(window_image(image, window, intercept, slope))

    return torch.stack(window_images)


def _read_image_3d(file_path: str, do_rotate=False):
    assert file_path is not None, 'file path is needed'
    assert os.path.isfile(file_path), 'wrong file path'

    image = torch.FloatTensor(nibabel.load(filename=file_path).get_fdata())
    if do_rotate:
        image = rotate(image.permute(2, 0, 1), 90).permute(1, 2, 0)
    return image


def _get_first_of_dicom_field_as_int(data):
    # credit: https://www.kaggle.com/code/dcstang/see-like-a-radiologist-with-systematic-windowing/notebook
    # get x[0] as in int is x is a 'pydicom.multival.MultiValue', otherwise get int(x)
    if type(data) == pydicom.multival.MultiValue:
        return int(data[0])
    else:
        return int(data)


def _get_windowing(data):
    # credit: https://www.kaggle.com/code/dcstang/see-like-a-radiologist-with-systematic-windowing/notebook
    dicom_fields = [data[('0028', '1050')].value,  # window center
                    data[('0028', '1051')].value,  # window width
                    data[('0028', '1052')].value,  # intercept
                    data[('0028', '1053')].value]  # slope
    return [_get_first_of_dicom_field_as_int(x) for x in dicom_fields]


def _read_image_2d(file_path: str):
    assert file_path is not None, 'file path is needed'
    assert os.path.isfile(file_path), 'wrong file path'

    image = pydicom.read_file(file_path)
    window_params = _get_windowing(image)
    image = torch.FloatTensor(image.pixel_array.astype(np.float32))
    return image, window_params
