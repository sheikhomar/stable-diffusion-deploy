import os
import time
from typing import List

import lightning as L
import requests
from lightning.app.frontend import StaticWebFrontend

from dream import DreamSlackCommandBot, StableDiffusionServe
from dream.components.load_balancer import LoadBalancer


class ReactUI(L.LightningFlow):
    def configure_layout(self):
        return StaticWebFrontend(os.path.join(os.path.dirname(__file__), "dream", "ui", "build"))


class RootWorkFlow(L.LightningFlow):

    AUTOSCALE_UP_THRESHOLD = 10
    AUTOSCALE_DOWN_THRESHOLD = 1
    MAX_WORKERS = 5

    """
    autoscale_interval: time in seconds in which autoscale will run
    """

    def __init__(
        self,
        initial_num_workers=5,
        autoscale_interval=1 * 30,
        max_batch_size=12,
        batch_size_wait_s=10,
        gpu_type="gpu-fast",
    ):
        super().__init__()
        self._initial_num_workers = self.num_workers = initial_num_workers
        self.autoscale_interval = autoscale_interval
        self.fake_trigger = 0
        self.gpu_type = gpu_type
        self._last_autoscale = time.time()
        self.load_balancer = LoadBalancer(
            max_wait_time=batch_size_wait_s, max_batch_size=max_batch_size, cache_calls=True, parallel=True
        )
        for i in range(initial_num_workers):
            work = StableDiffusionServe(cloud_compute=L.CloudCompute(gpu_type), cache_calls=True, parallel=True)
            setattr(self, f"serve_work_{i}", work)

        self.slack_bot = DreamSlackCommandBot(command="/dream")
        self.printed_url = False
        self.slack_bot_url = ""
        self.dream_url = ""
        self.ui = ReactUI()

    @property
    def model_servers(self) -> List[StableDiffusionServe]:
        works = []
        for i in range(self.num_workers):
            work: StableDiffusionServe = getattr(self, f"serve_work_{i}")
            works.append(work)
        return works

    def run(self):
        if os.environ.get("TESTING_LAI"):
            print("⚡ Lightning Dream App! ⚡")

        for model_serve in self.model_servers:
            model_serve.run()

        if all(model_serve.url for model_serve in self.model_servers):
            # run the load balancer when all the model server is ready
            self.load_balancer.run([serve.url for serve in self.model_servers])

        if self.load_balancer.url:  # hack for getting the work url
            self.dream_url = self.load_balancer.url
            if self.slack_bot is not None:
                self.slack_bot.run(self.load_balancer.url)
                self.slack_bot_url = self.slack_bot.url
                if self.slack_bot.url and not self.printed_url:
                    print("Slack Bot Work ready with URL=", self.slack_bot.url)
                    print("model serve url=", self.load_balancer.url)
                    self.printed_url = True

        if self.load_balancer.url:
            self.fake_trigger += 1
            if time.time() - self._last_autoscale < self.autoscale_interval:
                self.autoscale()

    def configure_layout(self):
        return [
            {
                "name": None,
                "content": self.ui,
            },
        ]

    def autoscale(self):
        """Upscale and down scale model inference works based on the number of requests."""
        num_requests = int(requests.get(f"{self.load_balancer.url}/num-requests").json())
        num_workers = len(self.model_servers)

        print("Number of requests: ", num_requests)
        print("Number of workers: ", self.num_workers)

        # based on @lantiga's impl: https://github.com/Lightning-AI/LAI-Stable-Diffusion-App/tree/scale_model_trial1
        # upscale
        if num_requests > self.AUTOSCALE_UP_THRESHOLD and num_workers < self.MAX_WORKERS:
            print(f"Upscale workers to {self.num_workers + 1}")
            work_index = len(self.model_servers)
            work = StableDiffusionServe(
                cloud_compute=L.CloudCompute(self.gpu_type),
                cache_calls=True,
                parallel=True,
            )
            setattr(self, f"serve_work_{work_index}", work)
            self.num_workers += 1
            self.load_balancer.update_servers(self.model_servers)

        # downscale
        elif num_requests < self.AUTOSCALE_DOWN_THRESHOLD and num_workers > self._initial_num_workers:
            print(f"Downscale workers to {self.num_workers - 1}")
            worker = self.model_servers[self.num_workers - 1]
            worker.stop()
            self.num_workers -= 1
            self.load_balancer.update_servers(self.model_servers)
        self._last_autoscale = time.time()


app = L.LightningApp(RootWorkFlow())
