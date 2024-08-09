import typing as T
from abc import ABC, abstractmethod

import torch
import torch.nn as nn
import pytorch_lightning as pl

from massspecgym.models.base import MassSpecGymModel
from massspecgym.simulation_utils.misc_utils import scatter_logl2normalize, scatter_logsumexp, safelog, \
    scatter_reduce
from massspecgym.simulation_utils.spec_utils import batched_bin_func, sparse_cosine_distance, \
    get_ints_transform_func, get_ints_untransform_func, batched_l1_normalize
from massspecgym.simulation_utils.nn_utils import build_lr_scheduler

class SimulationMassSpecGymModel(MassSpecGymModel, ABC):

    def __init__(
            self, 
            optimizer,
            lr,
            weight_decay,
            lr_schedule,
            ints_transform,
            mz_max,
            mz_bin_res,
            **kwargs):
        super().__init__(**kwargs)
        self.save_hyperparameters()
        self._setup_model()
        self._setup_loss_fn()
        self._setup_spec_fns()
        self._setup_metric_fns()
        self.metric_d = {}

    @abstractmethod
    def _setup_model(self):

        pass

    def configure_optimizers(self):

        if self.hparams.optimizer == "adam":
            optimizer_cls = torch.optim.Adam
        elif self.hparams.optimizer == "adamw":
            optimizer_cls = torch.optim.AdamW
        elif self.hparams.optimizer == "sgd":
            optimizer_cls = torch.optim.SGD
        else:
            raise ValueError(f"Unknown optimizer {self.optimizer}")
        optimizer = optimizer_cls(
            self.parameters(), 
            lr=self.hparams.lr, 
            weight_decay=self.hparams.weight_decay
        )
        ret = {
            "optimizer": optimizer,
        }
        if self.hparams.lr_schedule:
            scheduler = build_lr_scheduler(
                optimizer=optimizer, 
                decay_rate=self.hparams.lr_decay_rate, 
                warmup_steps=self.hparams.lr_warmup_steps,
                decay_steps=self.hparams.lr_decay_steps,
            )
            ret["lr_scheduler"] = {
                "scheduler": scheduler,
                "frequency": 1,
                "interval": "step",
            }
        return ret

    def _setup_spec_fns(self):

        self.ints_transform_func = get_ints_transform_func(self.hparams.ints_transform)
        self.ints_untransform_func = get_ints_untransform_func(self.hparams.ints_transform)
        self.ints_normalize_func = batched_l1_normalize

    def _preproc_spec(self,spec_mzs,spec_ints,spec_batch_idxs):

        # transform
        spec_ints = spec_ints * 1000.
        spec_ints = self.ints_transform_func(spec_ints)
        # renormalize
        spec_ints = self.ints_normalize_func(
            spec_ints,
            spec_batch_idxs
        )
        spec_ints = safelog(spec_ints)
        return spec_mzs, spec_ints, spec_batch_idxs

    def _setup_loss_fn(self):

        def _loss_fn(
            true_mzs: torch.Tensor, 
            true_logprobs: torch.Tensor,
            true_batch_idxs: torch.Tensor,
            pred_mzs: torch.Tensor,
            pred_logprobs: torch.Tensor,
            pred_batch_idxs: torch.Tensor
        ):

            cos_dist = sparse_cosine_distance(
                true_mzs=true_mzs,
                true_logprobs=true_logprobs,
                true_batch_idxs=true_batch_idxs,
                pred_mzs=pred_mzs,
                pred_logprobs=pred_logprobs,
                pred_batch_idxs=pred_batch_idxs,
                mz_max=self.hparams.mz_max,
                mz_bin_res=self.hparams.mz_bin_res
            )
            return cos_dist

        self.loss_fn = _loss_fn

    def get_cos_sim_fn(self, untransform: bool):

        def _cos_sim_fn(
            pred_mzs,
            pred_logprobs,
            pred_batch_idxs,
            true_mzs,
            true_logprobs,
            true_batch_idxs):

            if untransform:
                # untransform
                true_logprobs = safelog(self.ints_normalize_func(
                    self.ints_untransform_func(torch.exp(true_logprobs), true_batch_idxs), 
                    true_batch_idxs
                ))
                pred_logprobs = safelog(self.ints_normalize_func(
                    self.ints_untransform_func(torch.exp(pred_logprobs), pred_batch_idxs), 
                    pred_batch_idxs
                ))

            cos_sim = 1.-sparse_cosine_distance(
                pred_mzs=pred_mzs,
                pred_logprobs=pred_logprobs,
                pred_batch_idxs=pred_batch_idxs,
                true_mzs=true_mzs,
                true_logprobs=true_logprobs,
                true_batch_idxs=true_batch_idxs,
                mz_max=self.hparams.mz_max,
                mz_bin_res=self.hparams.mz_bin_res
            )

            return cos_sim
        
        return _cos_sim_fn

    def get_batch_metric_reduce_fn(self, sample_weight: bool):

        def _batch_metric_reduce(scores, weights):
            if not sample_weight:
                # ignore weights (uniform averaging)
                weights = torch.ones_like(weights)
            w_total = torch.sum(weights, dim=0)
            w_score_total = torch.sum(scores * weights, dim=0)
            w_mean = w_score_total / w_total
            return w_mean, w_score_total, w_total

        return _batch_metric_reduce

    def _setup_metric_fns(self):

        self.train_reduce_fn = self.get_batch_metric_reduce_fn(self.hparams.train_sample_weight)
        self.eval_reduce_fn = self.get_batch_metric_reduce_fn(self.hparams.eval_sample_weight)

        self.cos_sim_fn = self.get_cos_sim_fn(untransform=True)
        self.cos_sim_obj_fn = self.get_cos_sim_fn(untransform=False)

    def forward(self, **kwargs) -> dict:

        return self.model.forward(**kwargs)

    def step(self, batch: dict, metric_pref: str = "", stage=None) -> dict:

        true_mzs, true_logprobs, true_batch_idxs = self._preproc_spec(
            batch["spec_mzs"],
            batch["spec_ints"],
            batch["spec_batch_idxs"]
        )

        out_d = self.model.forward(
            **batch
        )
        pred_mzs = out_d["pred_mzs"]
        pred_logprobs = out_d["pred_logprobs"]
        pred_batch_idxs = out_d["pred_batch_idxs"]
        loss = self.loss_fn(
            true_mzs=true_mzs,
            true_logprobs=true_logprobs,
            true_batch_idxs=true_batch_idxs,
            pred_mzs=pred_mzs,
            pred_logprobs=pred_logprobs,
            pred_batch_idxs=pred_batch_idxs
        )
        reduce_fn = self.train_reduce_fn if metric_pref == "train_" else self.eval_reduce_fn
        mean_loss = reduce_fn(loss, batch["weight"])[0]
        batch_size = torch.max(pred_batch_idxs)+1

        # Log loss
        # TODO: not sure if this batch_size param messes up running total
        self.log(
            metric_pref + "loss_step",
            mean_loss,
            batch_size=batch_size,
            sync_dist=True,
            prog_bar=True,
            on_step=True,
            on_epoch=False
        )

        out_d = {
            "loss": mean_loss, 
            "pred_mzs": pred_mzs, 
            "pred_logprobs": pred_logprobs, 
            "pred_batch_idxs": pred_batch_idxs,
            "true_mzs": true_mzs,
            "true_logprobs": true_logprobs,
            "true_batch_idxs": true_batch_idxs}

        return out_d

    def on_batch_end(
        self, outputs: T.Any, batch: dict, batch_idx: int, metric_pref: str = "", stage=None
    ) -> None:
        """
        Compute evaluation metrics for the retrieval model based on the batch and corresponding predictions.
        This method will be used in the `on_train_batch_end`, `on_validation_batch_end`, since `on_test_batch_end` is
        overriden below.
        """
        pass
        # self.evaluate_cos_similarity_step(
        #     pred_mzs=outputs["pred_mzs"],
        #     pred_logpros=outputs["pred_logprobs"],
        #     pred_batch_idxs=outputs["pred_batch_idxs"],
        #     true_mzs=outputs["true_mzs"],
        #     true_logprobs=outputs["true_logprobs"],
        #     true_batch_idxs=outputs["true_batch_idxs"],
        #     metric_pref=metric_pref,
        # )

    def on_test_batch_end(
        self, outputs: T.Any, batch: dict, batch_idx: int
    ) -> None:
        metric_pref = "_test"
        self.evaluate_cos_similarity_step(
            outputs["spec_pred"],
            batch["spec"],
            metric_pref=metric_pref
        )
        # self.evaluate_hit_rate_step(
        #     outputs["spec_pred"],
        #     batch["spec"],
        #     metric_pref=metric_pref
        # )

    def evaluate_cos_similarity_step(
        self,
        specs_pred: torch.Tensor,
        specs: torch.Tensor,
        metric_pref: str = ""
    ) -> None:
        
        raise NotImplementedError

    def evaluate_hit_rate_step(
        self,
        pred_mzs: torch.Tensor,
        pred_logprobs: torch.Tensor,
        pred_batch_idxs: torch.Tensor,
        true_mzs: torch.Tensor,
        true_logprobs: torch.Tensor,
        true_batch_idxs: torch.Tensor,
        weight: torch.Tensor,
        metric_pref: str,
    ) -> None:
        """
        Evaulate Hit rate @ {1, 5, 20} (typically reported as Accuracy @ {1, 5, 20}).
        """
        
        raise NotImplementedError

    def on_train_epoch_end(self):

        train_metrics = {k: v for k, v in self.metric_d.items() if "train_" in k}
        for k, v in train_metrics.items():
            wmean_v = torch.sum(torch.stack(v[0])) / torch.sum(torch.stack(v[1]))
            self.log(
                k, 
                wmean_v, 
                sync_dist=True, 
                prog_bar=False,
            )
            del self.metric_d[k]

    def on_validation_epoch_end(self):

        val_metrics = {k: v for k, v in self.metric_d.items() if "val_" in k}
        for k, v in val_metrics.items():
            wmean_v = torch.sum(torch.stack(v[0])) / torch.sum(torch.stack(v[1]))
            self.log(
                k, 
                wmean_v,
                sync_dist=True, 
                prog_bar=False,
            )
            del self.metric_d[k]
