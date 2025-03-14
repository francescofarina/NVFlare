# Copyright (c) 2025, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import base64
from typing import Any, Dict

import torch
from executorch_export import export_model
from model import Net, TrainingNet
from torch import Tensor

from nvflare.apis.controller_spec import ClientTask, Task
from nvflare.apis.fl_constant import ReturnCode
from nvflare.apis.fl_context import FLContext
from nvflare.apis.impl.controller import Controller
from nvflare.apis.shareable import Shareable
from nvflare.apis.signal import Signal
from nvflare.app_common.app_constant import AppConstants
from nvflare.app_common.app_event_type import AppEventType
from nvflare.edge.aggregators.edge_json_accumulator import EdgeJsonAccumulator
from nvflare.security.logging import secure_format_exception

# Define the XOR dataset
X = torch.tensor([[0, 0], [0, 1], [1, 0], [1, 1]], dtype=torch.float32)
y = torch.tensor([0, 1, 1, 0], dtype=torch.float32)
# Import tensorboard
from torch.utils.tensorboard import SummaryWriter

tb_writer = SummaryWriter()


class EdgeExecutorchController(Controller):
    def __init__(
        self,
        num_rounds: int,
    ):
        super().__init__()
        self.model = TrainingNet(Net())
        self.input_tensor = torch.randn(1, 2)
        self.label_tensor = torch.ones(1, dtype=torch.int64)
        self.num_rounds = num_rounds
        self.current_round = None
        self.aggregator = None

    def start_controller(self, fl_ctx: FLContext) -> None:
        self.log_info(fl_ctx, "Initializing ExecuTorch mobile workflow.")
        self.aggregator = EdgeJsonAccumulator(aggr_key="data")

        # initialize global model
        fl_ctx.set_prop(AppConstants.START_ROUND, 1, private=True, sticky=True)
        fl_ctx.set_prop(AppConstants.NUM_ROUNDS, self.num_rounds, private=True, sticky=False)

    def stop_controller(self, fl_ctx: FLContext):
        self.log_info(fl_ctx, "Stopping ExecuTorch mobile workflow.")

    def _tensor_from_json(self, tensor_data: Dict[str, Any], divide_factor: int) -> Dict[str, Tensor]:
        """Convert JSON tensor data to PyTorch tensors."""
        grad_dict = {}
        for key, value in tensor_data.items():
            tensor = torch.Tensor(value["data"]).reshape(value["sizes"])
            grad_dict[key] = tensor / divide_factor
        print("get grad dict:", grad_dict)
        return grad_dict

    def _update_model(self, aggregated_grads: Dict[str, Tensor]) -> None:
        """Update model weights using aggregated gradients."""
        for key, param in self.model.state_dict().items():
            if key in aggregated_grads:
                self.model.state_dict()[key] -= aggregated_grads[key]

    def _export_current_model(self) -> bytes:
        """Export current model in ExecutorTorch format."""
        model_buffer = export_model(self.model, self.input_tensor, self.label_tensor).buffer
        base64_encoded = base64.b64encode(model_buffer).decode("utf-8")
        return base64_encoded

    def control_flow(self, abort_signal: Signal, fl_ctx: FLContext) -> None:
        try:
            self.log_info(fl_ctx, "Beginning Executorch mobile training phase.")

            fl_ctx.set_prop(AppConstants.NUM_ROUNDS, self.num_rounds, private=True, sticky=False)
            self.fire_event(AppEventType.TRAINING_STARTED, fl_ctx)

            for i in range(self.num_rounds):
                self.current_round = i
                if abort_signal.triggered:
                    return

                self.log_info(fl_ctx, f"Round {self.current_round} started.")
                fl_ctx.set_prop(AppConstants.CURRENT_ROUND, self.current_round, private=True, sticky=True)
                self.fire_event(AppEventType.ROUND_STARTED, fl_ctx)

                # Create task and send global model to clients
                encoded_buffer = self._export_current_model()

                # Compose shareable
                task_data = Shareable()
                model = {
                    "weights": encoded_buffer,
                    "format": "base64_encoded",
                    "input_dim": list(self.input_tensor.size()),
                    "label_dim": list(self.label_tensor.size()),
                }
                task_data["weights"] = model
                task_data["task_done"] = self.current_round >= (self.num_rounds - 1)
                task_data.set_header(AppConstants.CURRENT_ROUND, self.current_round)
                task_data.set_header(AppConstants.NUM_ROUNDS, self.num_rounds)
                task_data.add_cookie(AppConstants.CONTRIBUTION_ROUND, self.current_round)

                train_task = Task(
                    name="train",
                    data=task_data,
                    result_received_cb=self.process_train_result,
                )

                self.broadcast_and_wait(
                    task=train_task,
                    min_responses=2,
                    wait_time_after_min_received=10,
                    fl_ctx=fl_ctx,
                    abort_signal=abort_signal,
                )

                if abort_signal.triggered:
                    return

                self.log_info(fl_ctx, "Start aggregation.")
                self.fire_event(AppEventType.BEFORE_AGGREGATION, fl_ctx)
                aggr_result = self.aggregator.aggregate(fl_ctx)
                self.log_info(fl_ctx, f"Aggregation result: {aggr_result}")
                fl_ctx.set_prop(AppConstants.AGGREGATION_RESULT, aggr_result, private=True, sticky=False)
                self.fire_event(AppEventType.AFTER_AGGREGATION, fl_ctx)
                self.log_info(fl_ctx, "End aggregation.")

                # reset aggregator
                self.aggregator.reset(fl_ctx)

                # Convert aggregated gradients to PyTorch tensors
                divide_factor = aggr_result["num_devices"]
                aggregated_grads = self._tensor_from_json(aggr_result["weights"], divide_factor)
                self.log_info(fl_ctx, f"Aggregated gradients as Tensor: {aggregated_grads}")

                # Update model weights using aggregated gradients
                self._update_model(aggregated_grads)

                # Evaluate the model
                with torch.no_grad():
                    test_output = self.model.net(X).detach().argmax(dim=1)
                    # compute the accuracy
                    accuracy = (test_output == y).sum().item() / y.size(0)
                    tb_writer.add_scalar("acc", accuracy, i)

                if abort_signal.triggered:
                    return

            final_weights = self.model.state_dict()
            self.log_info(fl_ctx, f"Finished Mobile Training. Final weights: {final_weights}")
            # save the final model
            torch.save(self.model.state_dict(), "xor_model.pth")

        except Exception as e:
            error_msg = f"Exception in mobile control_flow: {secure_format_exception(e)}"
            self.log_exception(fl_ctx, error_msg)
            self.system_panic(error_msg, fl_ctx)

    def process_train_result(self, client_task: ClientTask, fl_ctx: FLContext) -> None:
        result = client_task.result
        client_name = client_task.client.name
        rc = result.get_return_code()

        # Raise errors if bad peer context or execution exception.
        if rc and rc != ReturnCode.OK:
            self.system_panic(
                f"Result from {client_name} is bad, error code: {rc}. "
                f"{self.__class__.__name__} exiting at round {self.current_round}.",
                fl_ctx=fl_ctx,
            )

            return

        self.log_info(fl_ctx, f"Weights: {result.get('weights', None)}")

        accepted = self.aggregator.accept(result, fl_ctx)
        accepted_msg = "ACCEPTED" if accepted else "REJECTED"
        self.log_info(
            fl_ctx, f"Contribution from {client_name} {accepted_msg} by the aggregator at round {self.current_round}."
        )
