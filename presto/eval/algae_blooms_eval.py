from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, cast

import numpy as np
import pandas as pd
import torch
from cropharvest.columns import RequiredColumns
from cropharvest.config import DAYS_PER_TIMESTEP
from cropharvest.eo import EarthEngineExporter
from google.cloud import storage
from sklearn.base import BaseEstimator
from sklearn.metrics import mean_squared_error, r2_score
from torch import nn
from torch.optim import AdamW
from torch.utils.data import DataLoader, TensorDataset
from tqdm import tqdm

from ..dataops import S1_S2_ERA5_SRTM, TAR_BUCKET
from ..model import FineTuningModel, Mosaiks1d, Seq2Seq
from ..presto import param_groups_lrd
from ..utils import data_dir, device
from .cropharvest_extensions import DynamicWorldExporter, Engineer
from .eval import EvalDataset

# At the time of export, the earliest S2 data
# on earthengine was 2015/6/23, so we will export
# any data from 3 months after this date. This
# yields 12267 / 17060 (72%) of datapoints
MIN_EXPORT_END_DATE = date(2016, 9, 23)
MIN_EXPORT_START_DATE = date(2015, 6, 23)
NUM_TIMESTEPS = 12
SURROUNDING_METRES = 80


class AlgaeBloomsEval(EvalDataset):

    regression = True
    num_outputs = 1

    def __init__(self) -> None:
        self.name = "AlgaeBlooms"
        self.labels = self.load_labels()

    @staticmethod
    def load_labels():
        region = "midwest"
        metadata = pd.read_csv(data_dir / "algae_blooms/metadata.csv")
        labels = pd.read_csv(data_dir / "algae_blooms/train_labels.csv")
        labels = labels.merge(metadata, on="uid")

        labels = labels[labels["region"] == region]

        labels[labels["split"] == "train"]

        labels.loc[(labels["longitude"] > -93) & (labels["latitude"] < 41), "split"] = "test"

        labels["date"] = pd.to_datetime(labels["date"])
        labels = labels[labels["date"] > str(MIN_EXPORT_END_DATE)]

        # add the labels required by the EarthEngine Exporter
        labels[RequiredColumns.LAT] = labels["latitude"]
        labels[RequiredColumns.LON] = labels["longitude"]
        labels["end_date"] = labels["date"]

        labels["start_date"] = labels["end_date"] - pd.Timedelta(
            NUM_TIMESTEPS * DAYS_PER_TIMESTEP, unit="d"
        )
        labels["start_date"] = labels["start_date"].clip(lower=pd.to_datetime(date(2015, 6, 23)))

        labels["export_identifier"] = labels["uid"]

        return labels

    @classmethod
    def export_satellite_data(cls) -> None:
        labels = cls.load_labels()
        exporter = EarthEngineExporter()
        exporter.export_for_labels(labels=labels)

    @classmethod
    def export_dynamic_world(cls):
        labels = cls.load_labels()
        exporter = DynamicWorldExporter()
        exporter.export_for_labels(labels=labels, surrounding_metres=SURROUNDING_METRES)

    @classmethod
    def tifs_to_npys(cls) -> None:
        npy_folder = data_dir / "algae_blooms/npy"
        # if any npy files exist, we will overwrite them
        for existing_npy_file in npy_folder.glob("*.npy"):
            existing_npy_file.unlink()

        labels = cls.load_labels()

        satellite_folder = data_dir / "algae_blooms/tifs/s1_s2_era5_srtm"
        dw_folder = data_dir / "algae_blooms/tifs/dynamic_world"
        x_list, dw_list, month_list = [], [], []
        latlon_list, is_test_list, uid_list, y_list = [], [], [], []
        for _, row in tqdm(labels.iterrows()):
            filename = f"{row['uid']}.tif"
            processed_arrays = Engineer.process_algal_bloom_files(
                satellite_folder / filename, dw_folder / filename, row, NUM_TIMESTEPS
            )

            if processed_arrays is not None:
                labelled_array, dw_array, months, y, latlon, is_test, uid = processed_arrays
                x_list.append(labelled_array)
                dw_list.append(dw_array)
                month_list.append(months)
                y_list.append(y)
                latlon_list.append(latlon)
                is_test_list.append(is_test)
                uid_list.append(uid)

        np.save(npy_folder / "s1_s2_era5_srtm.npy", np.stack(x_list))
        np.save(npy_folder / "dynamic_world.npy", np.stack(dw_list))
        np.save(npy_folder / "month.npy", np.stack(month_list))
        np.save(npy_folder / "target.npy", np.array(y_list))
        np.save(npy_folder / "latlon.npy", np.stack(latlon_list))
        np.save(npy_folder / "is_test.npy", np.array(is_test_list))
        np.save(npy_folder / "uid.npy", np.array(uid_list))

    @staticmethod
    def load_npy_gcloud(path: Path) -> np.ndarray:
        if not path.exists():
            blob = storage.Client().bucket(TAR_BUCKET).blob(f"eval/algae_blooms/npy/{path.name}")
            blob.download_to_filename(path)
        return np.load(path)

    @classmethod
    def load_npys(
        cls, test: bool
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        npy_folder = data_dir / "algae_blooms/npy"
        npy_folder.mkdir(exist_ok=True)
        is_test_np = cls.load_npy_gcloud(npy_folder / "is_test.npy")
        test_filter = is_test_np == (1 if test else 0)
        return (
            S1_S2_ERA5_SRTM.normalize(cls.load_npy_gcloud(npy_folder / "s1_s2_era5_srtm.npy"))[
                test_filter
            ],
            cls.load_npy_gcloud(npy_folder / "dynamic_world.npy")[test_filter],
            cls.load_npy_gcloud(npy_folder / "month.npy")[test_filter],
            cls.load_npy_gcloud(npy_folder / "target.npy")[test_filter],
            cls.load_npy_gcloud(npy_folder / "latlon.npy")[test_filter],
        )

    @torch.no_grad()
    def evaluate(
        self,
        finetuned_model: Union[FineTuningModel, BaseEstimator],
        pretrained_model=None,
        mask: Optional[np.ndarray] = None,
    ) -> Dict:

        batch_size = 64

        if isinstance(finetuned_model, BaseEstimator):
            assert isinstance(pretrained_model, (Seq2Seq, Mosaiks1d))

        x, dw, month, target, latlons = self.load_npys(test=True)

        dl = DataLoader(
            TensorDataset(
                torch.from_numpy(x).to(device).float(),
                torch.from_numpy(dw).to(device).long(),
                torch.from_numpy(month).to(device).long(),
                torch.from_numpy(latlons).to(device).float(),
            ),
            batch_size=batch_size,
            shuffle=False,
        )

        test_preds = []
        for (x, dw, month, latlons) in dl:
            batch_mask = self._mask_to_batch_tensor(mask, x.shape[0])
            if isinstance(finetuned_model, FineTuningModel):
                finetuned_model.eval()
                preds = (
                    finetuned_model(
                        x, dynamic_world=dw, mask=batch_mask, latlons=latlons, month=month
                    )
                    .squeeze(dim=1)
                    .cpu()
                    .numpy()
                )
            elif isinstance(finetuned_model, BaseEstimator):
                cast(Seq2Seq, pretrained_model).eval()
                encodings = (
                    cast(Seq2Seq, pretrained_model)
                    .encoder(x, dynamic_world=dw, mask=batch_mask, latlons=latlons, month=month)
                    .cpu()
                    .numpy()
                )
                preds = finetuned_model.predict(encodings)
            test_preds.append(preds)
        test_preds_np = np.concatenate(test_preds)

        prefix = finetuned_model.__class__.__name__
        return {
            f"algae_blooms_{prefix}_rmse": mean_squared_error(
                target, test_preds_np, squared=False
            ),
            f"algae_blooms_{prefix}_r2": r2_score(target, test_preds_np),
        }

    def finetune(self, pretrained_model, mask: Optional[np.ndarray] = None) -> FineTuningModel:
        # TODO - where are these controlled?
        lr, max_epochs, batch_size, patience = 3e-4, 200, 64, 10
        model = self._construct_finetuning_model(pretrained_model)

        parameters = param_groups_lrd(model)
        opt = AdamW(parameters, lr=lr)
        loss_fn = nn.HuberLoss(reduction="mean")

        x_np, dw_np, month_np, target_np, latlon_np = self.load_npys(test=False)

        val_filter = (latlon_np[:, 0] > 42.5) & (latlon_np[:, 1] < -92.5)

        train_dl = DataLoader(
            TensorDataset(
                torch.from_numpy(x_np[~val_filter]).to(device).float(),
                torch.from_numpy(dw_np[~val_filter]).to(device).long(),
                torch.from_numpy(target_np[~val_filter]).to(device).float(),
                torch.from_numpy(month_np[~val_filter]).to(device).long(),
                torch.from_numpy(latlon_np[~val_filter]).to(device).float(),
            ),
            batch_size=batch_size,
            shuffle=True,
        )

        val_dl = DataLoader(
            TensorDataset(
                torch.from_numpy(x_np[val_filter]).to(device).float(),
                torch.from_numpy(dw_np[val_filter]).to(device).long(),
                torch.from_numpy(target_np[val_filter]).to(device).float(),
                torch.from_numpy(month_np[val_filter]).to(device).long(),
                torch.from_numpy(latlon_np[val_filter]).to(device).float(),
            ),
            batch_size=batch_size,
            shuffle=False,
        )

        train_loss = []
        val_loss = []
        best_loss = None
        best_model_dict = None
        epochs_since_improvement = 0
        for _ in tqdm(range(max_epochs), desc="Finetuning"):
            model.train()
            epoch_train_loss = 0.0
            for (x, dw, target, month, latlons) in tqdm(train_dl, desc="Training", leave=False):
                opt.zero_grad()
                batch_mask = self._mask_to_batch_tensor(mask, x.shape[0])
                preds = model(x, dynamic_world=dw, mask=batch_mask, latlons=latlons, month=month)
                loss = loss_fn(preds.flatten(), target)

                epoch_train_loss += loss.item()
                loss.backward()
                opt.step()
            train_loss.append(epoch_train_loss / len(train_dl))

            model.eval()
            all_preds, all_y = [], []
            for x, dw, y, month, latlons in val_dl:
                batch_mask = self._mask_to_batch_tensor(mask, x.shape[0])
                with torch.no_grad():
                    preds = model(
                        x,
                        dynamic_world=dw,
                        mask=batch_mask,
                        latlons=latlons,
                        month=month,
                    )
                    all_preds.append(preds.cpu().numpy())
                    all_y.append(y.cpu().numpy())

            val_loss.append(mean_squared_error(np.concatenate(all_y), np.concatenate(all_preds)))
            if best_loss is None:
                best_loss = val_loss[-1]
                best_model_dict = model.state_dict()
            else:
                if val_loss[-1] < best_loss:
                    best_loss = val_loss[-1]
                    best_model_dict = model.state_dict()
                    epochs_since_improvement = 0
                else:
                    epochs_since_improvement += 1
                    if epochs_since_improvement >= patience:
                        print("Early stopping!")
                        break
        assert best_model_dict is not None
        model.load_state_dict(best_model_dict)

        model.eval()
        return model

    def finetuning_results(
        self,
        pretrained_model,
        model_modes: List[str],
        mask: Optional[np.ndarray] = None,
    ) -> Dict:
        results_dict = {}
        for model_mode in model_modes:
            assert model_mode in ["finetune", "Regression", "Random Forest"]
        if "finetune" in model_modes:
            model = self.finetune(pretrained_model, mask)
            results_dict.update(self.evaluate(model, None, mask))

        sklearn_modes = [x for x in model_modes if x != "finetune"]
        if len(sklearn_modes) > 0:
            x, dw, month, target, latlons = self.load_npys(test=False)
            sklearn_models = self.finetune_sklearn_model(
                x,
                target,
                pretrained_model,
                dynamic_world=dw,
                latlons=latlons,
                mask=mask,
                month=month,
                models=sklearn_modes,
            )
            for sklearn_model in sklearn_models:
                results_dict.update(self.evaluate(sklearn_model, pretrained_model, mask))
        return results_dict
