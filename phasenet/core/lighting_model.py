from os.path import join
from typing import Dict

import pytorch_lightning as pl
import torch
import torch.nn as nn
from phasenet.conf import Config
from phasenet.core.sgram import GenSgram
from phasenet.model.unet import UNet
from phasenet.utils.visualize import VisualizeInfo
from pytorch_lightning.utilities import rank_zero_only
from torch.utils.tensorboard import SummaryWriter


class PhaseNetModel(pl.LightningModule):
    def __init__(self, model: nn.Module, conf: Config) -> None:
        super().__init__()
        # * load confs
        self.spec_conf = conf.spectrogram
        self.model_conf = conf.model
        self.train_conf = conf.train
        self.visualize_conf = conf.visualize

        # * define the model
        self.sgram_trans = GenSgram(self.spec_conf)
        # self.model = model(self.model_conf)
        self.model = None
        if model == UNet:
            self.model = UNet(
                features=self.model_conf.init_features,
                in_cha=self.model_conf.in_channels,
                out_cha=self.model_conf.out_channels,
                first_layer_repeating_cnn=self.model_conf.first_layer_repeating_cnn,
                n_freq=self.model_conf.n_freq,
                ksize_down=self.model_conf.encoder_conv_kernel_size,
                ksize_up=self.model_conf.decoder_conv_kernel_size
            )
        # * figure logger
        self.show_figs = VisualizeInfo(
            phases=conf.data.phases,
            sampling_rate=conf.spectrogram.sampling_rate,
            x_range=[0, conf.data.win_length],
            freq_range=[conf.spectrogram.freqmin, conf.spectrogram.freqmax],
            global_max=False,
            sgram_threshold=conf.visualize.sgram_threshold,
        )
        self.figs_train_store = []
        self.figs_val_store = []
        self.figs_test_store = []

    def training_step(self, batch: Dict, batch_idx: int) -> torch.Tensor:
        wave, label = batch["data"], batch["label"]
        sgram = self.sgram_trans(wave)
        output = self.model(sgram)
        predict = output['predict']
        loss = self._criterion(predict, label)
        # * logging
        # refer to https://github.com/PyTorchLightning/pytorch-lightning/issues/10349
        self.log_dict({"Loss/train": loss, "step": self.current_epoch + 1.0},
                      on_step=False, on_epoch=True, batch_size=len(wave), prog_bar=True)
        self._log_figs_train(batch, batch_idx, sgram, predict)
        # * return misfit
        return loss

    def validation_step(self, batch: Dict, batch_idx: int) -> torch.Tensor:
        loss, sgram, predict = self._shared_eval_step(batch, batch_idx)
        self.log_dict({"Loss/validation": loss, "step": self.current_epoch + 1.0}, on_step=False,
                      on_epoch=True, batch_size=len(batch['data']), prog_bar=True)
        self._log_figs_val(batch, batch_idx, sgram, predict)
        return loss

    def test_step(self, batch: Dict, batch_idx: int) -> torch.Tensor:
        loss, sgram, predict = self._shared_eval_step(batch, batch_idx)
        self.log("Test Loss", loss, on_step=False,
                 on_epoch=True, batch_size=len(batch['data']))
        self._log_figs_test(batch, batch_idx, sgram, predict)
        return loss

    def _shared_eval_step(self, batch: Dict, batch_idx: int) -> torch.Tensor:
        wave, label = batch["data"], batch["label"]
        sgram = self.sgram_trans(wave)
        output = self.model(sgram)
        predict = output['predict']
        loss = self._criterion(predict, label)
        return loss, sgram, predict

    def _criterion(self, inputs: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        loss = nn.functional.kl_div(
            torch.nn.functional.log_softmax(inputs, dim=1), target, reduction='batchmean',
        )
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(
            self.parameters(), lr=self.train_conf.learning_rate, weight_decay=self.train_conf.weight_decay, amsgrad=False
        )
        lr_scheduler = torch.optim.lr_scheduler.LinearLR(
            optimizer, start_factor=1, end_factor=0.1, total_iters=self._num_training_steps)
        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": lr_scheduler,
                "interval": "step"
            }
        }

    # * ============== helpers ============== * #
    @property
    def _num_training_steps(self) -> int:
        """Total training steps inferred from datamodule and devices."""
        # from https://github.com/PyTorchLightning/pytorch-lightning/issues/5449
        if self.trainer.max_steps != -1:
            return self.trainer.max_steps

        limit_batches = self.trainer.limit_train_batches
        batches = len(
            self.trainer._data_connector._train_dataloader_source.dataloader())
        batches = min(batches, limit_batches) if isinstance(
            limit_batches, int) else int(limit_batches * batches)

        effective_accum = self.trainer.accumulate_grad_batches * self.trainer.num_devices
        return (batches // effective_accum) * self.trainer.max_epochs

    # * ============== figure plotting ============== * #
    @rank_zero_only
    def _log_figs_train(self, batch: Dict, batch_idx: int, sgram: torch.Tensor, predict: torch.Tensor) -> None:
        if not self.visualize_conf.log_train:
            return
        if (self.current_epoch == self.trainer.max_epochs-1) or (self.visualize_conf.log_epoch and (self.current_epoch+1) % self.visualize_conf.log_epoch == 0):
            batch_size = len(sgram)
            finished_examples = batch_size*batch_idx
            if finished_examples < self.visualize_conf.example_num:
                if finished_examples+batch_size < self.visualize_conf.example_num:
                    example_this_batch = batch_size
                    last_step = False
                else:
                    example_this_batch = self.visualize_conf.example_num-finished_examples
                    last_step = True

                predict_freq = torch.nn.functional.softmax(predict, dim=1)
                figs = self.show_figs(
                    batch, sgram, predict_freq, example_this_batch)
                self.figs_train_store.extend(figs)
                if last_step:
                    tensorboard: SummaryWriter = self.logger.experiment
                    if self.current_epoch == self.trainer.max_epochs-1:
                        tag = "train/final"
                    elif self.visualize_conf.log_epoch and (self.current_epoch+1) % self.visualize_conf.log_epoch == 0:
                        tag = f"train/epoch{self.current_epoch+1}"
                    tensorboard.add_figure(
                        tag, self.figs_train_store, global_step=self.current_epoch+1)
                    self.figs_train_store = []

    @rank_zero_only
    def _log_figs_val(self, batch: Dict, batch_idx: int, sgram: torch.Tensor, predict: torch.Tensor) -> None:
        if not self.visualize_conf.log_val:
            return
        if (self.current_epoch == self.trainer.max_epochs-1) or (self.visualize_conf.log_epoch and (self.current_epoch+1) % self.visualize_conf.log_epoch == 0):
            batch_size = len(sgram)
            finished_examples = batch_size*batch_idx
            if finished_examples < self.visualize_conf.example_num:
                if finished_examples+batch_size < self.visualize_conf.example_num:
                    example_this_batch = batch_size
                    last_step = False
                else:
                    example_this_batch = self.visualize_conf.example_num-finished_examples
                    last_step = True

                predict_freq = torch.nn.functional.softmax(predict, dim=1)
                figs = self.show_figs(
                    batch, sgram, predict_freq, example_this_batch)
                self.figs_val_store.extend(figs)
                if last_step:
                    tensorboard: SummaryWriter = self.logger.experiment
                    if self.current_epoch == self.trainer.max_epochs-1:
                        tag = "validation/final"
                    elif self.visualize_conf.log_epoch and (self.current_epoch+1) % self.visualize_conf.log_epoch == 0:
                        tag = f"validation/epoch{self.current_epoch+1}"
                    tensorboard.add_figure(
                        tag, self.figs_val_store, global_step=self.current_epoch+1)
                    self.figs_val_store = []

    @rank_zero_only
    def _log_figs_test(self, batch: Dict, batch_idx: int, sgram: torch.Tensor, predict: torch.Tensor) -> None:
        if not self.visualize_conf.log_test:
            return
        batch_size = len(sgram)
        finished_examples = batch_size*batch_idx
        if finished_examples < self.visualize_conf.example_num:
            if finished_examples+batch_size < self.visualize_conf.example_num:
                example_this_batch = batch_size
                last_step = False
            else:
                example_this_batch = self.visualize_conf.example_num-finished_examples
                last_step = True

            predict_freq = torch.nn.functional.softmax(predict, dim=1)
            figs = self.show_figs(
                batch, sgram, predict_freq, example_this_batch)
            self.figs_test_store.extend(figs)
            if last_step:
                tensorboard: SummaryWriter = self.logger.experiment
                if self.visualize_conf.log_test_seprate_folder:
                    for idx, each_fig in enumerate(self.figs_test_store):
                        each_fig.savefig(
                            join(self.visualize_conf.log_test_seprate_folder_path, f"{idx+1}.eps"))
                tensorboard.add_figure(
                    "test/final", self.figs_test_store, global_step=self.current_epoch+1)
                self.figs_test_store = []
