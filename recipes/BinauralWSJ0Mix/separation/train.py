#!/usr/bin/env/python3
"""Recipe for training a neural speech separation system on wsjmix the
dataset. The system employs an encoder, a decoder, and a masking network.

To run this recipe, do the following:
> python train.py hparams/sepformer.yaml
> python train.py hparams/dualpath_rnn.yaml
> python train.py hparams/convtasnet.yaml

The experiment file is flexible enough to support different neural
networks. By properly changing the parameter files, you can try
different architectures. The script supports both wsj2mix and
wsj3mix.


Authors
 * Cem Subakan 2020
 * Mirco Ravanelli 2020
 * Samuele Cornell 2020
 * Mirko Bronzi 2020
 * Jianyuan Zhong 2020
 * Zijian Huang 2022
"""

import os
import sys
import torch
import torch.nn.functional as F
import torchaudio
import speechbrain as sb
import speechbrain.nnet.schedulers as schedulers
from speechbrain.utils.distributed import run_on_main
from torch.cuda.amp import autocast
from hyperpyyaml import load_hyperpyyaml
import numpy as np
from tqdm import tqdm
import csv
import logging
from pyroomacoustics.experimental.localization import tdoa
from speechbrain.processing.features import STFT, spectral_magnitude
from torch.nn import Conv1d
from speechbrain.pretrained.fetching import fetch
import zipfile


# Define training procedure
class Separation(sb.Brain):
    def compute_forward(self, mix, targets, stage, noise=None):
        """Forward computations from the mixture to the separated signals."""

        # Unpack lists and put tensors in the right device
        mix, mix_lens = mix
        mix, mix_lens = mix.to(self.device), mix_lens.to(self.device)

        # Convert targets to tensor
        targets = torch.cat(
            [targets[i][0].unsqueeze(-1) for i in range(self.hparams.num_spks)],
            dim=-1,
        ).to(self.device)
        # [1,t,2,2] dim = -1 is num_speakers

        # Add speech distortions
        if stage == sb.Stage.TRAIN:
            with torch.no_grad():
                if self.hparams.use_speedperturb or self.hparams.use_rand_shift:
                    mix, targets = self.add_speed_perturb(targets, mix_lens)

                    mix = targets.sum(-1)

                if self.hparams.use_wavedrop:
                    mix = self.hparams.wavedrop(mix, mix_lens)

                if self.hparams.limit_training_signal_len:
                    mix, targets = self.cut_signals(mix, targets)

        # Separation
        if "independent" in self.hparams.experiment_name:
            mix_wl = self.hparams.EncoderL(mix[:, :, 0])
            est_maskl = self.hparams.MaskNetL(mix_wl)
            mix_wl = torch.stack([mix_wl] * self.hparams.num_spks)
            sep_hl = mix_wl * est_maskl

            mix_wr = self.hparams.EncoderR(mix[:, :, 1])
            est_maskr = self.hparams.MaskNetR(mix_wr)
            mix_wr = torch.stack([mix_wr] * self.hparams.num_spks)
            sep_hr = mix_wr * est_maskr
        elif "cross" in self.hparams.experiment_name:
            EPS = 1e-8
            compute_stft = STFT(
                sample_rate=self.hparams.sample_rate,
                win_length=256 * 1000.0 / self.hparams.sample_rate,
                hop_length=128 * 1000.0 / self.hparams.sample_rate,
                n_fft=256,
            ).to(self.device)
            mix_stft = compute_stft(mix).permute(-1, 0, 2, 1, 3)
            # IPD = torch.atan2(mix_stft[:, :, :, :, 1], mix_stft[:, :, :, :, 0])
            # sinIPD = torch.sin(IPD[0] - IPD[1])
            # cosIPD = torch.cos(IPD[0] - IPD[1])
            ILD_beforelog = spectral_magnitude(mix_stft[0], power=0.5) / (
                spectral_magnitude(mix_stft[1], power=0.5) + EPS
            )
            ILD = 10 * torch.log10(ILD_beforelog + EPS)
            # print(ILD.shape) # [1,129,t/win]

            # Separation
            mix_wl = self.hparams.EncoderL(mix[:, :, 0])
            # [1,64,t/k]
            n_samples = mix_wl.shape[-1]
            ILD_upsample = F.interpolate(ILD, size=n_samples)
            conv1 = Conv1d(
                ILD_upsample.shape[1], mix_wl.shape[1], kernel_size=1
            ).to(self.device)
            ILD_cat = conv1(ILD_upsample)

            mix_catl = torch.cat((mix_wl, ILD_cat), dim=1)
            est_maskl = self.hparams.MaskNetL(mix_catl)
            mix_wl = torch.stack([mix_wl] * self.hparams.num_spks)
            sep_hl = mix_wl * torch.chunk(est_maskl, 2, dim=2)[0]

            mix_wr = self.hparams.EncoderR(mix[:, :, 1])
            mix_catr = torch.cat((mix_wr, -ILD_cat), dim=1)
            est_maskr = self.hparams.MaskNetR(mix_catr)
            mix_wr = torch.stack([mix_wr] * self.hparams.num_spks)
            sep_hr = mix_wr * torch.chunk(est_maskr, 2, dim=2)[0]
        elif "parallel" in self.hparams.experiment_name:
            mix_wl1 = self.hparams.EncoderL(mix[:, :, 0])
            mix_wr2 = self.hparams.EncoderR(mix[:, :, 1])
            mix_wl = torch.cat((mix_wl1, mix_wr2), dim=1)

            est_maskl = self.hparams.MaskNetL(mix_wl)
            mix_wl1 = torch.stack([mix_wl1] * self.hparams.num_spks)
            mix_wr2 = torch.stack([mix_wr2] * self.hparams.num_spks)
            sep_hl1 = mix_wl1 * torch.chunk(est_maskl, 2, dim=2)[0]
            sep_hr2 = mix_wr2 * torch.chunk(est_maskl, 2, dim=2)[1]

            mix_wl2 = self.hparams.EncoderR(mix[:, :, 0])
            mix_wr1 = self.hparams.EncoderL(mix[:, :, 1])
            mix_wr = torch.cat((mix_wl2, mix_wr1), dim=1)

            est_maskr = self.hparams.MaskNetR(mix_wr)
            mix_wl2 = torch.stack([mix_wl2] * self.hparams.num_spks)
            mix_wr1 = torch.stack([mix_wr1] * self.hparams.num_spks)
            sep_hl2 = mix_wl2 * torch.chunk(est_maskr, 2, dim=2)[0]
            sep_hr1 = mix_wr1 * torch.chunk(est_maskr, 2, dim=2)[1]
            sep_hl = sep_hl1 + sep_hr2
            sep_hr = sep_hl2 + sep_hr1
        else:
            raise ValueError(
                "Experiment name in hparams should contain one of these--'independent', 'cross', and 'parallel'."
            )

        # Decoding
        est_sourcel = torch.cat(
            [
                self.hparams.DecoderL(sep_hl[i]).unsqueeze(-1)
                for i in range(self.hparams.num_spks)
            ],
            dim=-1,
        )

        est_sourcer = torch.cat(
            [
                self.hparams.DecoderR(sep_hr[i]).unsqueeze(-1)
                for i in range(self.hparams.num_spks)
            ],
            dim=-1,
        )

        est_source = torch.cat(
            [est_sourcel.unsqueeze(-2), est_sourcer.unsqueeze(-2)], dim=-2
        )
        # T changed after conv1d in encoder, fix it here
        T_origin = mix.size(1)
        T_est = est_source.size(1)
        if T_origin > T_est:
            est_source = F.pad(est_source, (0, 0, 0, 0, 0, T_origin - T_est))
        else:
            est_source = est_source[:, :T_origin, :]

        return est_source, targets

    def compute_objectives(self, predictions, targets):
        """Computes the snr loss"""
        return self.hparams.loss(targets, predictions)

    def fit_batch(self, batch):
        """Trains one batch"""
        # Unpacking batch list
        mixture = batch.mix_sig
        targets = [batch.s1_sig, batch.s2_sig]

        if self.hparams.num_spks == 3:
            targets.append(batch.s3_sig)

        if self.auto_mix_prec:
            with autocast():
                predictions, targets = self.compute_forward(
                    mixture, targets, sb.Stage.TRAIN
                )
                loss = self.compute_objectives(predictions, targets)

                # hard threshold the easy dataitems
                if self.hparams.threshold_byloss:
                    th = self.hparams.threshold
                    loss_to_keep = loss[loss > th]
                    if loss_to_keep.nelement() > 0:
                        loss = loss_to_keep.mean()
                else:
                    loss = loss.mean()

            if (
                loss < self.hparams.loss_upper_lim and loss.nelement() > 0
            ):  # the fix for computational problems
                self.scaler.scale(loss).backward()
                if self.hparams.clip_grad_norm >= 0:
                    self.scaler.unscale_(self.optimizer)
                    torch.nn.utils.clip_grad_norm_(
                        self.modules.parameters(), self.hparams.clip_grad_norm,
                    )
                self.scaler.step(self.optimizer)
                self.scaler.update()
            else:
                self.nonfinite_count += 1
                logger.info(
                    "infinite loss or empty loss! it happened {} times so far - skipping this batch".format(
                        self.nonfinite_count
                    )
                )
                loss.data = torch.tensor(0).to(self.device)
        else:
            predictions, targets = self.compute_forward(
                mixture, targets, sb.Stage.TRAIN
            )
            loss = self.compute_objectives(predictions, targets)

            if self.hparams.threshold_byloss:
                th = self.hparams.threshold
                loss_to_keep = loss[loss > th]
                if loss_to_keep.nelement() > 0:
                    loss = loss_to_keep.mean()
            else:
                loss = loss.mean()

            if (
                loss < self.hparams.loss_upper_lim and loss.nelement() > 0
            ):  # the fix for computational problems
                loss.backward()
                if self.hparams.clip_grad_norm >= 0:
                    torch.nn.utils.clip_grad_norm_(
                        self.modules.parameters(), self.hparams.clip_grad_norm
                    )
                self.optimizer.step()
            else:
                self.nonfinite_count += 1
                logger.info(
                    "infinite loss or empty loss! it happened {} times so far - skipping this batch".format(
                        self.nonfinite_count
                    )
                )
                loss.data = torch.tensor(0).to(self.device)
        self.optimizer.zero_grad()

        return loss.detach().cpu()

    def evaluate_batch(self, batch, stage):
        """Computations needed for validation/test batches"""
        snt_id = batch.id
        mixture = batch.mix_sig
        targets = [batch.s1_sig, batch.s2_sig]
        if self.hparams.num_spks == 3:
            targets.append(batch.s3_sig)

        with torch.no_grad():
            predictions, targets = self.compute_forward(mixture, targets, stage)
            loss = self.compute_objectives(predictions, targets)

        # Manage audio file saving
        if stage == sb.Stage.TEST and self.hparams.save_audio:
            if hasattr(self.hparams, "n_audio_to_save"):
                if self.hparams.n_audio_to_save > 0:
                    self.save_audio(snt_id[0], mixture, targets, predictions)
                    self.hparams.n_audio_to_save += -1
            else:
                self.save_audio(snt_id[0], mixture, targets, predictions)

        return loss.detach()

    def on_stage_end(self, stage, stage_loss, epoch):
        """Gets called at the end of a epoch."""
        # Compute/store important stats
        stage_stats = {"snr": stage_loss}
        if stage == sb.Stage.TRAIN:
            self.train_stats = stage_stats

        # Perform end-of-iteration things, like annealing, logging, etc.
        if stage == sb.Stage.VALID:

            # Learning rate annealing
            if isinstance(
                self.hparams.lr_scheduler, schedulers.ReduceLROnPlateau
            ):
                current_lr, next_lr = self.hparams.lr_scheduler(
                    [self.optimizer], epoch, stage_loss
                )
                schedulers.update_learning_rate(self.optimizer, next_lr)
            else:
                # if we do not use the reducelronplateau, we do not change the lr
                current_lr = self.hparams.optimizer.optim.param_groups[0]["lr"]

            self.hparams.train_logger.log_stats(
                stats_meta={"epoch": epoch, "lr": current_lr},
                train_stats=self.train_stats,
                valid_stats=stage_stats,
            )
            self.checkpointer.save_and_keep_only(
                meta={"snr": stage_stats["snr"]}, min_keys=["snr"],
            )
        elif stage == sb.Stage.TEST:
            self.hparams.train_logger.log_stats(
                stats_meta={"Epoch loaded": self.hparams.epoch_counter.current},
                test_stats=stage_stats,
            )

    def add_speed_perturb(self, targets, targ_lens):
        """Adds speed perturbation and random_shift to the input signals"""

        min_len = -1
        recombine = False

        if self.hparams.use_speedperturb:
            # Performing speed change (independently on each source)
            new_targets = []
            recombine = True

            for i in range(targets.shape[-1]):
                new_target = self.hparams.speedperturb(
                    targets[:, :, :, i], targ_lens
                )
                new_targets.append(new_target)
                if i == 0:
                    min_len = new_target.shape[1]
                else:
                    if new_target.shape[1] < min_len:
                        min_len = new_target.shape[1]

            if self.hparams.use_rand_shift:
                # Performing random_shift (independently on each source)
                recombine = True
                for i in range(targets.shape[-1]):
                    rand_shift = torch.randint(
                        self.hparams.min_shift, self.hparams.max_shift, (1,)
                    )
                    new_targets[i] = new_targets[i].to(self.device)
                    new_targets[i] = torch.roll(
                        new_targets[i], shifts=(rand_shift[0],), dims=1
                    )

            # Re-combination
            if recombine:
                if self.hparams.use_speedperturb:
                    targets = torch.zeros(
                        targets.shape[0],
                        min_len,
                        targets.shape[-2],
                        targets.shape[-1],
                        device=targets.device,
                        dtype=torch.float,
                    )
                for i, new_target in enumerate(new_targets):
                    targets[:, :, :, i] = new_targets[i][:, 0:min_len]

        mix = targets.sum(-1)
        return mix, targets

    def cut_signals(self, mixture, targets):
        """This function selects a random segment of a given length within the mixture.
        The corresponding targets are selected accordingly"""
        randstart = torch.randint(
            0,
            1 + max(0, mixture.shape[1] - self.hparams.training_signal_len),
            (1,),
        ).item()
        targets = targets[
            :, randstart : randstart + self.hparams.training_signal_len, :
        ]
        mixture = mixture[
            :, randstart : randstart + self.hparams.training_signal_len
        ]
        return mixture, targets

    def reset_layer_recursively(self, layer):
        """Reinitializes the parameters of the neural networks"""
        if hasattr(layer, "reset_parameters"):
            layer.reset_parameters()
        for child_layer in layer.modules():
            if layer != child_layer:
                self.reset_layer_recursively(child_layer)

    def cal_interaural_error(self, predictions, targets):
        """Compute ITD and ILD errors"""

        EPS = 1e-8
        s_target = targets[0]  # [T,E,C]
        s_prediction = predictions[0]  # [T,E,C]

        # ITD is computed with generalized cross-correlation phase transform (GCC-PHAT)
        ITD_target = [
            tdoa(
                s_target[:, 0, i].cpu().numpy(),
                s_target[:, 1, i].cpu().numpy(),
                fs=self.hparams.sample_rate,
            )
            * 10 ** 6
            for i in range(s_target.shape[-1])
        ]
        ITD_prediction = [
            tdoa(
                s_prediction[:, 0, i].cpu().numpy(),
                s_prediction[:, 1, i].cpu().numpy(),
                fs=self.hparams.sample_rate,
            )
            * 10 ** 6
            for i in range(s_prediction.shape[-1])
        ]
        ITD_error1 = np.mean(
            np.abs(np.array(ITD_target) - np.array(ITD_prediction))
        )
        ITD_error2 = np.mean(
            np.abs(np.array(ITD_target) - np.array(ITD_prediction)[::-1])
        )
        ITD_error = min(ITD_error1, ITD_error2)

        # ILD  = 10 * log_10(||s_left||^2 / ||s_right||^2)
        ILD_target_beforelog = torch.sum(s_target[:, 0] ** 2, dim=0) / (
            torch.sum(s_target[:, 1] ** 2, dim=0) + EPS
        )
        ILD_target = 10 * torch.log10(ILD_target_beforelog + EPS)  # [C]
        ILD_prediction_beforelog = torch.sum(s_prediction[:, 0] ** 2, dim=0) / (
            torch.sum(s_prediction[:, 1] ** 2, dim=0) + EPS
        )
        ILD_prediction = 10 * torch.log10(ILD_prediction_beforelog + EPS)  # [C]

        ILD_error1 = torch.mean(torch.abs(ILD_target - ILD_prediction))
        ILD_error2 = torch.mean(torch.abs(ILD_target - ILD_prediction.flip(0)))
        ILD_error = min(ILD_error1.item(), ILD_error2.item())

        return ITD_error, ILD_error

    def save_results(self, test_data):
        """This script computes the SDR and SI-SNR metrics and saves
        them into a csv file"""

        # Create folders where to store audio
        save_file = os.path.join(self.hparams.output_folder, "test_results.csv")

        # Variable init
        all_snrs = []
        all_snrs_i = []
        all_delta_ITDs = []
        all_delta_ILDs = []
        csv_columns = ["snt_id", "snr", "snr_i", "delta_ITD", "delta_ILD"]

        test_loader = sb.dataio.dataloader.make_dataloader(
            test_data, **self.hparams.dataloader_opts
        )

        with open(save_file, "w") as results_csv:
            writer = csv.DictWriter(results_csv, fieldnames=csv_columns)
            writer.writeheader()

            # Loop over all test sentence
            with tqdm(test_loader, dynamic_ncols=True) as t:
                for i, batch in enumerate(t):

                    # Apply Separation
                    mixture, mix_len = batch.mix_sig
                    snt_id = batch.id
                    targets = [batch.s1_sig, batch.s2_sig]
                    if self.hparams.num_spks == 3:
                        targets.append(batch.s3_sig)

                    with torch.no_grad():
                        predictions, targets = self.compute_forward(
                            batch.mix_sig, targets, sb.Stage.TEST
                        )

                    # Compute SNR
                    snr = self.compute_objectives(predictions, targets)

                    # Compute SNR improvement
                    mixture_signal = torch.stack(
                        [mixture] * self.hparams.num_spks, dim=-1
                    )
                    mixture_signal = mixture_signal.to(targets.device)
                    snr_baseline = self.compute_objectives(
                        mixture_signal, targets
                    )
                    snr_i = snr - snr_baseline

                    # Compute ITD and ILD
                    delta_ITD, delta_ILD = self.cal_interaural_error(
                        predictions, targets
                    )

                    # Saving on a csv file
                    row = {
                        "snt_id": snt_id[0],
                        "snr": -snr.item(),
                        "snr_i": -snr_i.item(),
                        "delta_ITD": delta_ITD,
                        "delta_ILD": delta_ILD,
                    }
                    writer.writerow(row)

                    # Metric Accumulation
                    all_snrs.append(-snr.item())
                    all_snrs_i.append(-snr_i.item())
                    all_delta_ITDs.append(delta_ITD)
                    all_delta_ILDs.append(delta_ILD)

                row = {
                    "snt_id": "avg",
                    "snr": np.array(all_snrs).mean(),
                    "snr_i": np.array(all_snrs_i).mean(),
                    "delta_ITD": np.array(all_delta_ITDs).mean(),
                    "delta_ILD": np.array(all_delta_ILDs).mean(),
                }
                writer.writerow(row)

        logger.info("Mean SNR is {}".format(np.array(all_snrs).mean()))
        logger.info("Mean SNRi is {}".format(np.array(all_snrs_i).mean()))
        logger.info(
            "Mean Delta ITD is {}".format(np.array(all_delta_ITDs).mean())
        )
        logger.info(
            "Mean Delta ILD is {}".format(np.array(all_delta_ILDs).mean())
        )

    def save_audio(self, snt_id, mixture, targets, predictions):
        "saves the test audio (mixture, targets, and estimated sources) on disk"

        # Create outout folder
        save_path = os.path.join(self.hparams.save_folder, "audio_results")
        if not os.path.exists(save_path):
            os.mkdir(save_path)

        for ns in range(self.hparams.num_spks):

            # Estimated source
            signal = predictions[0, :, :, ns]
            signal = signal / signal.abs().max(0).values
            save_file = os.path.join(
                save_path, "item{}_source{}hat.wav".format(snt_id, ns + 1)
            )
            torchaudio.save(
                save_file, signal.permute(1, 0).cpu(), self.hparams.sample_rate
            )

            # Original source
            signal = targets[0, :, :, ns]
            signal = signal / signal.abs().max(0).values
            save_file = os.path.join(
                save_path, "item{}_source{}.wav".format(snt_id, ns + 1)
            )
            torchaudio.save(
                save_file, signal.permute(1, 0).cpu(), self.hparams.sample_rate
            )

        # Mixture
        signal = mixture[0][0, :]
        signal = signal / signal.abs().max(0).values
        save_file = os.path.join(save_path, "item{}_mix.wav".format(snt_id))
        torchaudio.save(
            save_file, signal.permute(1, 0).cpu(), self.hparams.sample_rate
        )


if __name__ == "__main__":

    # Load hyperparameters file with command-line overrides
    hparams_file, run_opts, overrides = sb.parse_arguments(sys.argv[1:])
    with open(hparams_file) as fin:
        hparams = load_hyperpyyaml(fin, overrides)

    # Initialize ddp (useful only for multi-GPU DDP training)
    sb.utils.distributed.ddp_init_group(run_opts)

    # Logger info
    logger = logging.getLogger(__name__)

    # Create experiment directory
    sb.create_experiment_directory(
        experiment_directory=hparams["output_folder"],
        hyperparams_to_save=hparams_file,
        overrides=overrides,
    )

    # Check if wsj0_tr is set with dynamic mixing
    if hparams["dynamic_mixing"] and not os.path.exists(
        hparams["base_folder_dm"]
    ):
        print(
            "Please, specify a valid base_folder_dm folder when using dynamic mixing"
        )
        sys.exit(1)

    if not os.path.exists(hparams["datasets_generation"]):
        print("Download Datasets Generation scripts")
        fetch(
            filename="main.zip",
            source="https://github.com/huangzj421/Binaural-WSJ0Mix/archive/refs/heads",
            savedir=hparams["data_folder"],
            save_filename="Binaural-WSJ0Mix-main.zip",
        )
        file = zipfile.ZipFile(
            os.path.join(hparams["data_folder"], "Binaural-WSJ0Mix-main.zip")
        )
        file.extractall(path=hparams["data_folder"])

    if not os.path.exists(os.path.join(hparams["data_folder"], "wav8k")):
        print("Generate Binaural WSJ0Mix dataset automatically")
        sys.path.append(hparams["datasets_generation"])
        if hparams["num_spks"] == 2:
            from create_wav_2speakers import create_binaural_wsj0mix
        else:
            from create_wav_3speakers import create_binaural_wsj0mix
        run_on_main(
            create_binaural_wsj0mix,
            kwargs={
                "wsj_root": hparams["wsj_root"],
                "output_root": hparams["data_folder"],
                "datafreqs": hparams["data_freqs"],
                "datamodes": hparams["data_modes"],
            },
        )

    # Data preparation
    from recipes.BinauralWSJ0Mix.prepare_data import (
        prepare_binaural_wsj0mix,
    )  # noqa

    run_on_main(
        prepare_binaural_wsj0mix,
        kwargs={
            "datapath": hparams["data_folder"],
            "savepath": hparams["save_folder"],
            "n_spks": hparams["num_spks"],
            "skip_prep": hparams["skip_prep"],
            "fs": hparams["sample_rate"],
        },
    )

    # Create dataset objects
    from recipes.WSJ0Mix.separation.train import dataio_prep

    if hparams["dynamic_mixing"]:
        from dynamic_mixing import dynamic_mix_data_prep

        # if the base_folder for dm is not processed, preprocess them
        if "processed" not in hparams["base_folder_dm"]:
            # if the processed folder already exists we just use it otherwise we do the preprocessing
            if not os.path.exists(
                os.path.normpath(hparams["base_folder_dm"]) + "_processed"
            ):
                from recipes.WSJ0Mix.meta.preprocess_dynamic_mixing import (
                    resample_folder,
                )

                print("Resampling the base folder")
                run_on_main(
                    resample_folder,
                    kwargs={
                        "input_folder": hparams["base_folder_dm"],
                        "output_folder": os.path.normpath(
                            hparams["base_folder_dm"]
                        )
                        + "_processed",
                        "fs": hparams["sample_rate"],
                        "regex": "**/*.wav",
                    },
                )
                # adjust the base_folder_dm path
                hparams["base_folder_dm"] = (
                    os.path.normpath(hparams["base_folder_dm"]) + "_processed"
                )
            else:
                print(
                    "Using the existing processed folder on the same directory as base_folder_dm"
                )
                hparams["base_folder_dm"] = (
                    os.path.normpath(hparams["base_folder_dm"]) + "_processed"
                )

        train_data = dynamic_mix_data_prep(hparams)
        _, valid_data, test_data = dataio_prep(hparams)
    else:
        train_data, valid_data, test_data = dataio_prep(hparams)

    # Load pretrained model if pretrained_separator is present in the yaml
    if "pretrained_separator" in hparams:
        run_on_main(hparams["pretrained_separator"].collect_files)
        hparams["pretrained_separator"].load_collected()

    # Brain class initialization
    separator = Separation(
        modules=hparams["modules"],
        opt_class=hparams["optimizer"],
        hparams=hparams,
        run_opts=run_opts,
        checkpointer=hparams["checkpointer"],
    )

    # re-initialize the parameters if we don't use a pretrained model
    if "pretrained_separator" not in hparams:
        for module in separator.modules.values():
            separator.reset_layer_recursively(module)

    if not hparams["test_only"]:
        # Training
        separator.fit(
            separator.hparams.epoch_counter,
            train_data,
            valid_data,
            train_loader_kwargs=hparams["dataloader_opts"],
            valid_loader_kwargs=hparams["dataloader_opts"],
        )

    # Eval
    separator.evaluate(test_data, min_key="snr")
    separator.save_results(test_data)
