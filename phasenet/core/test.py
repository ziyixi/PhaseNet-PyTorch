from typing import Callable, Optional

import torch
import torch.nn as nn
from torch.utils.data import DataLoader


def test_one_epoch(model: nn.Module,
                   criterion: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
                   test_loader: DataLoader,
                   use_amp: bool,
                   device: torch.device,
                   log_predict: bool = False) -> Optional[dict]:
    model.eval()
    loss_log = torch.zeros(1, device=device)
    if log_predict:
        predict_log = []
    with torch.inference_mode():
        for meta in test_loader:
            # * forward
            sgram, target = meta['sgram'].to(device), meta['label'].to(device)
            if use_amp:
                with torch.cuda.amp.autocast(enabled=True):
                    output = model(sgram)
                    predict = output['predict']
                    loss = criterion(predict, target)
            else:
                output = model(sgram)
                predict = output['predict']
                loss = criterion(predict, target)
            loss_log += loss.detach()
            if log_predict:
                predict_log.append(
                    torch.nn.functional.softmax(predict.detach(), dim=1))

    res = {
        "loss_mean": loss_log/len(test_loader),
    }
    if log_predict:
        res['predict'] = predict_log
    return res
