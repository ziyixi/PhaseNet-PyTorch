"""
dataset.py

convert the asdf file with the data information to pytorch's dataset
expected asdf file:
1. data streams are stored in waveforms, tagged with raw_recording
2. the phase arrival time and the reference time are stored in auxiliary
"""
from os.path import isfile, join
from typing import Dict, List

import torch
from obspy import UTCDateTime
from phasenet.conf.load_conf import DataConfig
from pyasdf import ASDFDataSet
from torch.utils.data import Dataset


class WaveFormDataset(Dataset):
    """
    Waveform dataset and phase arrival time tag.
    """

    def __init__(self, data_conf: DataConfig, data_type: str = "train", transform=None, stack_transform=None, prepare: bool = False) -> None:
        super().__init__()
        self.data_conf = data_conf
        self.data_type = data_type
        self.transform = transform
        self.stack_transform = stack_transform

        # path related
        asdf_file_path = ""
        if self.data_type == "train":
            asdf_file_path = join(data_conf.data_dir, data_conf.train)
        elif self.data_type == "val":
            asdf_file_path = join(data_conf.data_dir, data_conf.val)
        elif self.data_type == "test":
            asdf_file_path = join(data_conf.data_dir, data_conf.test)
        else:
            raise Exception("data type must be train, val, or test!")

        cache_path = asdf_file_path+".pt"
        # to prepare data
        self.data: Dict[str, torch.Tensor] = {}
        # left and right, comparing with data, nearby
        self.left_data: Dict[str, torch.Tensor] = {}
        self.right_data: Dict[str, torch.Tensor] = {}
        self.label: Dict[str, torch.Tensor] = {}
        self.wave_keys: List[str] = []

        if prepare:
            # load files and save to torch cache
            # if has already been prepared
            if isfile(cache_path):
                return
            # prefetch data
            with ASDFDataSet(asdf_file_path, mode="r") as ds:
                self.wave_keys = ds.waveforms.list()
                aux_keys = [item.replace('.', "_") for item in self.wave_keys]
                for wk, ak in zip(self.wave_keys, aux_keys):
                    self.add_data(ds, wk, ak)
            # save cache
            self.save(cache_path)

        else:
            # already saved, now the fetch stage
            if not isfile(cache_path):
                raise Exception(f"cache path {cache_path} has no file.")
            self.load(cache_path)

    def add_data(self, ds: ASDFDataSet, wk: str, ak: str) -> None:
        # * handle times
        # add label
        arrival_times: List[float] = []
        for phase in self.data_conf.phases:
            arrival_times.append(ds.auxiliary_data[phase][ak].data[:][0])
        start = min(arrival_times)-self.data_conf.left_extend
        end = min(arrival_times)+self.data_conf.right_extend
        if start < 0:
            # smaller than start time, reset it to 0
            # as tetsed, we always have start<=tp<=ts<=end
            end += -start
            start = 0
        left_signal_start = start-self.data_conf.win_length
        left_signal_end = start
        right_signal_start = end
        right_signal_end = end+self.data_conf.win_length
        # update arrival_times based on start
        arrival_times = [item-start for item in arrival_times]
        # cut the dataset based on ref time
        ref_time = UTCDateTime(
            ds.auxiliary_data["REFTIME"][ak].data[:][0])
        start, end = ref_time+start, ref_time+end
        left_signal_start, left_signal_end = ref_time + \
            left_signal_start, ref_time+left_signal_end
        right_signal_start, right_signal_end = ref_time + \
            right_signal_start, ref_time+right_signal_end
        # * handle slices
        stream = ds.waveforms[wk].raw_recording
        # here we assume sampling_rate should be the same
        sampling_rate: float = stream[0].stats.sampling_rate

        res = torch.zeros(
            3, int(sampling_rate*self.data_conf.win_length))
        left_res = torch.zeros(
            3, int(sampling_rate*self.data_conf.win_length))
        right_res = torch.zeros(
            3, int(sampling_rate*self.data_conf.win_length))
        components = ["R", "T", "Z"]
        for i in range(3):
            trace = stream.select(component=components[i])[0]
            if start < trace.stats.starttime or end > trace.stats.endtime or trace.stats.endtime-self.data_conf.win_length < end:
                # both signal and noise should be able to cut
                raise Exception(
                    f"{wk} has incorrect time or its length is too small")
            # signal processing
            trace.detrend()
            trace.taper(max_percentage=self.data_conf.taper_percentage)
            trace.filter('bandpass', freqmin=self.data_conf.filter_freqmin, freqmax=self.data_conf.filter_freqmax,
                         corners=self.data_conf.filter_corners, zerophase=self.data_conf.filter_zerophase)
            # cut
            wave = trace.slice(starttime=start, endtime=end)
            left_wave = trace.slice(
                starttime=left_signal_start, endtime=left_signal_end)
            right_wave = trace.slice(
                starttime=right_signal_start, endtime=right_signal_end)
            # to torch
            wave_data = torch.from_numpy(
                wave.data)
            res[i, :] = wave_data[:res.shape[1]]
            # left wave might not be that long
            left_wave_data = torch.from_numpy(left_wave.data)
            left_wave_data = left_wave_data[:left_res.shape[1]]
            left_res[i, -len(left_wave_data):] = left_wave_data[:]
            # right wave is reliable
            right_wave_data = torch.from_numpy(
                right_wave.data)
            right_res[i, :] = right_wave_data[:right_res.shape[1]]

        # update arrivals to idx of points
        arrival_times = [round(item*sampling_rate) for item in arrival_times]

        self.data[wk] = res
        self.left_data[wk] = left_res
        self.right_data[wk] = right_res
        self.label[wk] = torch.tensor(arrival_times, dtype=torch.int)

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, idx: int, stack_main: bool = True) -> Dict:
        # dict
        key = self.wave_keys[idx]
        sample = {
            "data": self.data[key],
            "left_data": self.left_data[key],
            "right_data": self.right_data[key],
            "arrivals": self.label[key],
            "key": key
        }
        if self.transform:
            sample = self.transform(sample)
        if stack_main and self.stack_transform:
            random_idx = torch.randint(len(self.data), (1,)).item()
            while random_idx == idx:
                random_idx = torch.randint(len(self.data), (1,)).item()
            random_sample = self.__getitem__(random_idx, stack_main=False)
            if torch.rand(1).item() <= self.data_conf.stack_ratio:
                # stack based on the ratio
                sample = self.stack_transform(sample, random_sample)
        return sample

    def save(self, file_name: str) -> None:
        # save the dataset to pt files
        tosave = {
            "wave_keys": self.wave_keys,
            "data": self.data,
            "left_data": self.left_data,
            "right_data": self.right_data,
            "label": self.label
        }
        torch.save(tosave, file_name)

    def load(self, file_name: str) -> None:
        # load the data from pt files
        toload = torch.load(file_name)
        self.wave_keys: List[str] = toload['wave_keys']
        self.data: Dict[str, torch.Tensor] = toload['data']
        self.left_data: Dict[str, torch.Tensor] = toload['left_data']
        self.right_data: Dict[str, torch.Tensor] = toload['right_data']
        self.label: Dict[str, torch.Tensor] = toload['label']
