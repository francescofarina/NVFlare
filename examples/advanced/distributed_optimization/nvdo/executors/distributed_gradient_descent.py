import torch
from nvflare.apis.dxo import from_shareable
from nvdo.utils.metrics import compute_loss_over_dataset
import time

from .base import SynchronousAlgorithmExecutor
from abc import abstractmethod


class DGDExecutor(SynchronousAlgorithmExecutor):
    @abstractmethod
    def __init__(
        self,
        model: torch.nn.Module | None = None,
        loss: torch.nn.modules.loss._Loss | None = None,
        train_dataloader: torch.utils.data.DataLoader | None = None,
        test_dataloader: torch.utils.data.DataLoader | None = None,
        val_dataloader: torch.utils.data.DataLoader | None = None,
    ):
        super().__init__()
        self.model = model
        self.loss = loss
        self.train_dataloader = train_dataloader
        self.test_dataloader = test_dataloader
        self.val_dataloader = val_dataloader

        # metrics
        self.train_loss_sequence = []
        self.test_loss_sequence = []

    def run_algorithm(self, fl_ctx, shareable, abort_signal):
        start_time = time.time()
        iter_dataloader = iter(self.train_dataloader)

        for iteration in range(self._iterations):
            self.log_info(fl_ctx, f"iteration: {iteration}/{self._iterations}")
            if abort_signal.triggered:
                break

            try:
                data, label = next(iter_dataloader)
            except StopIteration:
                # 3. store metrics
                current_time = time.time() - start_time
                self.train_loss_sequence.append(
                    (
                        current_time,
                        compute_loss_over_dataset(
                            self.model, self.loss, self.train_dataloader
                        ),
                    )
                )
                self.test_loss_sequence.append(
                    (
                        current_time,
                        compute_loss_over_dataset(
                            self.model, self.loss, self.test_dataloader
                        ),
                    )
                )
                # restart after an epoch
                iter_dataloader = iter(self.train_dataloader)
                data, label = next(iter_dataloader)

            # run algorithm step
            # 1. exchange values
            with torch.no_grad():
                self._exchange_values(
                    fl_ctx, value=self.model.parameters(), iteration=iteration
                )

                # compute consensus value
                for idx, param in enumerate(self.model.parameters()):
                    if param.requires_grad:
                        param.mul_(self._weight)
                        for neighbor in self.neighbors:
                            param.add_(
                                self.neighbors_values[iteration][neighbor.id][idx],
                                alpha=neighbor.weight,
                            )
            # 2. update current value
            self.model.zero_grad()
            pred = self.model(data)
            loss = self.loss(pred, label)
            loss.backward()

            with torch.no_grad():
                for param in self.model.parameters():
                    if param.grad is not None:
                        param.add_(param.grad, alpha=-self._stepsize)

            # free memory that's no longer needed
            del self.neighbors_values[iteration]

    def _to_message(self, x):
        return [
            param.cpu().numpy() for param in iter(x) if param.requires_grad
        ]

    def _from_message(self, x):
        return [torch.from_numpy(param) for param in x]
    
    def _pre_algorithm_run(self, fl_ctx, shareable, abort_signal):
        self._iterations = from_shareable(shareable).data["iterations"]
        self._stepsize = from_shareable(shareable).data["stepsize"]

        init_train_loss = compute_loss_over_dataset(
            self.model, self.loss, self.train_dataloader
        )
        init_test_loss = compute_loss_over_dataset(
            self.model, self.loss, self.test_dataloader
        )

        self.train_loss_sequence.append((0, init_train_loss))
        self.test_loss_sequence.append((0, init_test_loss))

    def _post_algorithm_run(self, *args, **kwargs):
        torch.save(torch.tensor(self.train_loss_sequence), "train_loss_sequence.pt")
        torch.save(torch.tensor(self.test_loss_sequence), "test_loss_sequence.pt")
