import pickle
from addict import Dict

import numpy as np
import torch
from torch.utils.data import Dataset
from torch.nn.utils.rnn import pad_sequence


class ContactDataset(Dataset):
    SET_VALUED_FIELDS = [
        "encounter_health",
        "encounter_message",
        "encounter_partner_id",
        "encounter_day",
        "encounter_is_contagion",
    ]

    def __init__(self, path: str):
        """
        Parameters
        ----------
        path : str
            Path to the pickle file.
        """
        # Private
        self._num_id_bits = 16
        # Public
        self.path = path
        # Prepwork
        self._read_data()

    def _read_data(self):
        with open(self.path, "rb") as f:
            data = pickle.load(f)
        # Access with data[human_idx, day_idx]
        # noinspection PyTypeChecker
        self.data: np.ndarray = np.asarray(list(zip(*data)))

    @property
    def num_humans(self):
        return len(self.data)

    @property
    def num_days(self):
        return len(self.data[0])

    def __len__(self):
        return self.num_humans * self.num_days

    def get(self, human_idx: int, day_idx: int) -> Dict:
        """
        Parameters
        ----------
        human_idx : int
            Index specifying the human
        day_idx : int
            Index of the day

        Returns
        -------
        Dict
            An addict with the following attributes:
                -> `health_history`: 14-day health history of self of shape (14, 13)
                        with channels `reported_symptoms` (12), `test_results`(1).
                -> `history_days`: time-stamps to go with the health_history.
                -> `current_compartment`: current epidemic compartment (S/E/I/R)
                    of shape (4,).
                -> `infectiousness_history`: 14-day history of infectiousness,
                    of shape (14, 1).
                -> `encounter_health`: health during encounter, of shape (M, 13)
                -> `encounter_message`: risk transmitted during encounter, of shape (M, 8).
                        These are the 8 bits of info that can be sent between users.
                -> `encounter_partner_id`: id of the other in the encounter,
                        of shape (M, num_id_bits). If num_id_bits = 16, it means that the
                        id (say 65535) is represented in 16-bit binary.
                -> `encounter_day`: the day of encounter, of shape (M, 1)
                -> `encounter_is_contagion`: whether the encounter was a contagion.
        """
        human_day = self.data[human_idx, day_idx]
        human_day_info = next(iter(human_day.values()))
        # -------- Encounters --------
        # Extract info about encounters
        #   encounter_info.shape = M3, where M is the number of encounters.
        encounter_info = human_day_info["observed"]["candidate_encounters"]
        encounter_partner_id, encounter_message, encounter_day = (
            encounter_info[:, 0],
            encounter_info[:, 1],
            encounter_info[:, 2],
        )
        num_encounters = encounter_info.shape[0]
        # Convert partner-id's to binary (shape = (M, num_id_bits))
        encounter_partner_id = (
            np.unpackbits(
                encounter_partner_id.astype(f"uint{self._num_id_bits}").view("uint8")
            )
            .reshape(num_encounters, -1)
            .astype("float32")
        )
        # Convert risk
        encounter_message = (
            np.unpackbits(encounter_message.astype("uint8"))
            .reshape(num_encounters, -1)
            .astype("float32")
        )
        encounter_is_contagion = human_day_info["unobserved"][
            "exposure_encounter"
        ].astype("float32")
        encounter_day = encounter_day.astype("float32")
        # -------- Health --------
        # Get health info
        health_history = np.concatenate(
            [
                human_day_info["observed"]["reported_symptoms"],
                human_day_info["observed"]["test_results"][:, None],
            ],
            axis=1,
        )
        infectiousness_history = human_day_info["unobserved"]["infectiousness"][:, None]
        history_days = np.clip(np.arange(day_idx - 13, day_idx + 1), 0, None)[:, None]
        hdi_to_health_at_encounter = lambda hdi: np.concatenate(
            [
                hdi["observed"]["reported_symptoms"][0],
                hdi["observed"]["test_results"][0:1],
            ],
            axis=0,
        ).astype("float32")
        # Get historical health info given the day of encounter (shape = (M, 13))
        health_at_encounter = np.array(
            [
                hdi_to_health_at_encounter(next(iter(_human_day_info.values())))
                for _human_day_info in self.data[human_idx, encounter_day.astype("int")]
            ]
        )
        if human_day_info["unobserved"]["is_recovered"]:
            current_compartment = "R"
        elif human_day_info["unobserved"]["is_infectious"]:
            current_compartment = "I"
        elif human_day_info["unobserved"]["is_exposed"]:
            current_compartment = "E"
        else:
            current_compartment = "R"
        current_compartment = np.array(
            [
                current_compartment == "S",
                current_compartment == "E",
                current_compartment == "I",
                current_compartment == "R",
            ]
        ).astype("float32")
        # This should be it
        return Dict(
            health_history=torch.from_numpy(health_history).float(),
            infectiousness_history=torch.from_numpy(infectiousness_history).float(),
            history_days=torch.from_numpy(history_days).float(),
            current_compartment=torch.from_numpy(current_compartment).float(),
            encounter_health=torch.from_numpy(health_at_encounter).float(),
            encounter_message=torch.from_numpy(encounter_message).float(),
            encounter_partner_id=torch.from_numpy(encounter_partner_id).float(),
            encounter_day=torch.from_numpy(encounter_day[:, None]).float(),
            encounter_is_contagion=torch.from_numpy(encounter_is_contagion).float(),
        )

    def __getitem__(self, item):
        human_idx, day_idx = np.unravel_index(item, (self.num_humans, self.num_days))
        return self.get(human_idx, day_idx)

    @classmethod
    def collate_fn(cls, batch):
        fixed_size_collates = {
            key: torch.stack([x[key] for x in batch], dim=0)
            for key in batch[0].keys()
            if key not in cls.SET_VALUED_FIELDS
        }

        # Make a mask
        max_set_len = max([x[cls.SET_VALUED_FIELDS[0]].shape[0] for x in batch])
        set_lens = torch.tensor([x[cls.SET_VALUED_FIELDS[0]].shape[0] for x in batch])
        mask = (
            torch.arange(max_set_len, dtype=torch.long)
            .expand(len(batch), max_set_len)
            .lt(set_lens[:, None])
        ).float()
        # Pad the set elements by writing in place to pre-made tensors
        padded_collates = {
            key: pad_sequence([x[key] for x in batch], batch_first=True)
            for key in cls.SET_VALUED_FIELDS
        }
        # Make the final addict and return
        collates = Dict(mask=mask)
        collates.update(fixed_size_collates)
        collates.update(padded_collates)
        return collates


def _test_loader():
    from torch.utils.data.dataloader import DataLoader

    path = "/Users/nrahaman/Python/ctt/data/output.pkl"
    dataset = ContactDataset(path)
    dataloader = DataLoader(dataset, batch_size=5, collate_fn=ContactDataset.collate_fn)
    batch = next(iter(dataloader))


def _test_dataset():
    path = "/Users/nrahaman/Python/ctt/data/output.pkl"
    dataset = ContactDataset(path)
    sample = dataset.get(0, 25)


if __name__ == "__main__":
    # _test_loader()
    # _test_dataset()
    pass