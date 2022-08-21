import torch
from torchmetrics import Metric
from typing import List


class MetricBase(Metric):
    full_state_update = False

    def __init__(self, phase_idx: int, threshold: int, time_point_length: int):
        super().__init__()
        self.phase_idx = phase_idx
        self.threshold = threshold
        self.time_point_length = time_point_length

        self.add_state("tp", default=torch.tensor(0),
                       dist_reduce_fx="sum")
        self.add_state("t", default=torch.tensor(0),
                       dist_reduce_fx="sum")
        self.add_state("p", default=torch.tensor(0),
                       dist_reduce_fx="sum")

    def update(self, predict_arrivals: List[List[List]], real_arrivals: torch.Tensor):
        # predict_arrivals is the peaks idx from utils.extract_peaks, with the shape (len_batch,len_phases,len_peaks)
        # real_arrivals is the real peaks idx, with the shape (len_batch,len_phases)
        batch_size = len(predict_arrivals)
        for ibatch in range(batch_size):
            real_arrival = real_arrivals[ibatch][self.phase_idx].detach(
            ).cpu().item()
            predict_arrival_list = predict_arrivals[ibatch][self.phase_idx]
            matched = 0
            # if real_arrival is out of bound, we should not count it as real_arrival
            for predict_arrival in predict_arrival_list:
                if abs(real_arrival-predict_arrival) < self.threshold:
                    matched = 1
                    break
            self.tp += matched  # true positive
            self.t += 1 if 0 < real_arrival < self.time_point_length else 0  # true
            self.p += len(predict_arrival_list)  # positive


class Precision(MetricBase):
    def __init__(self, phase_idx: int, threshold: int, time_point_length: int):
        super().__init__(phase_idx, threshold, time_point_length)

    def compute(self):
        return self.tp.float()/self.p if self.p != 0 else torch.tensor(0.)


class Recall(MetricBase):
    def __init__(self, phase_idx: int, threshold: int, time_point_length: int):
        super().__init__(phase_idx, threshold, time_point_length)

    def compute(self):
        return self.tp.float()/self.t if self.t != 0 else torch.tensor(0.)


class F1(MetricBase):
    def __init__(self, phase_idx: int, threshold: int, time_point_length: int):
        super().__init__(phase_idx, threshold, time_point_length)

    def compute(self):
        precision = self.tp.float()/self.p if self.p != 0 else torch.tensor(0.)
        recall = self.tp.float()/self.t if self.t != 0 else torch.tensor(0.)
        return 2 * precision * recall / (precision + recall) if precision + recall != 0 else torch.tensor(0.)
