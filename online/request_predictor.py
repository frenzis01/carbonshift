"""
Request Predictor Module

Provides forecasting of future request arrivals for online scheduling.
Supports mock patterns and extensible to ML-based predictors.
"""

from typing import List, Dict, Any
from dataclasses import dataclass
import random
import math


@dataclass
class PredictedRequest:
    """Predicted request with estimated arrival time and deadline"""
    id: str
    arrival_time: int
    deadline: int
    estimated: bool = True  # Flag to distinguish from real requests


class RequestPredictor:
    """
    Base class for request arrival prediction.
    Subclasses implement specific forecasting methods.
    """

    def predict_load(self, time_slot: int) -> float:
        """
        Predict number of requests arriving in time_slot.

        Args:
            time_slot: Time slot index (e.g., hour of day)

        Returns:
            Expected number of requests
        """
        raise NotImplementedError

    def predict_requests(self, start_slot: int, end_slot: int) -> List[PredictedRequest]:
        """
        Generate predicted requests for time window [start_slot, end_slot].

        Args:
            start_slot: Start of prediction window
            end_slot: End of prediction window (inclusive)

        Returns:
            List of predicted requests with arrival times and deadlines
        """
        raise NotImplementedError


class MockRequestPredictor(RequestPredictor):
    """
    Mock predictor with realistic diurnal pattern:
    - Morning peak (8-10 AM)
    - Evening peak (18-20 PM)
    - Low activity at night (2-5 AM)
    - Random noise for variability

    Pattern simulates typical workload: high during business hours,
    peaks at commute times, low at night.
    """

    def __init__(
        self,
        base_load: float = 500.0,
        morning_peak_hour: int = 9,
        evening_peak_hour: int = 19,
        peak_multiplier: float = 2.5,
        night_multiplier: float = 0.2,
        noise_stddev: float = 0.15,
        seed: int = None
    ):
        """
        Initialize mock predictor with diurnal pattern parameters.

        Args:
            base_load: Baseline number of requests per hour
            morning_peak_hour: Hour for morning peak (0-23)
            evening_peak_hour: Hour for evening peak (0-23)
            peak_multiplier: Multiplier for peak hours (e.g., 2.5x base)
            night_multiplier: Multiplier for night hours (e.g., 0.2x base)
            noise_stddev: Standard deviation of Gaussian noise (fraction of mean)
            seed: Random seed for reproducibility
        """
        self.base_load = base_load
        self.morning_peak_hour = morning_peak_hour
        self.evening_peak_hour = evening_peak_hour
        self.peak_multiplier = peak_multiplier
        self.night_multiplier = night_multiplier
        self.noise_stddev = noise_stddev

        if seed is not None:
            random.seed(seed)

    def _get_hourly_pattern(self, hour: int) -> float:
        """
        Calculate deterministic load pattern for given hour.

        Uses bimodal Gaussian mixture:
        - Morning peak centered at morning_peak_hour
        - Evening peak centered at evening_peak_hour
        - Night valley (2-5 AM)

        Args:
            hour: Hour of day (0-23)

        Returns:
            Multiplier for base_load (e.g., 2.5 for peak, 0.2 for night)
        """
        # Normalize hour to [0, 23]
        hour = hour % 24

        # Bimodal Gaussian components
        morning_peak = self._gaussian(hour, self.morning_peak_hour, sigma=2.0)
        evening_peak = self._gaussian(hour, self.evening_peak_hour, sigma=2.0)

        # Night valley (minimum at 3 AM)
        night_valley = self._gaussian(hour, 3, sigma=3.0)

        # Combine: peaks boost, night valley reduces
        pattern = (
            1.0 +  # Baseline
            (self.peak_multiplier - 1.0) * (morning_peak + evening_peak) -
            (1.0 - self.night_multiplier) * night_valley
        )

        # Clamp to reasonable range
        return max(self.night_multiplier, min(self.peak_multiplier, pattern))

    @staticmethod
    def _gaussian(x: float, center: float, sigma: float) -> float:
        """Gaussian function (unnormalized, peak=1.0)"""
        return math.exp(-((x - center) ** 2) / (2 * sigma ** 2))

    def predict_load(self, time_slot: int) -> float:
        """
        Predict request load for time_slot with noise.

        Args:
            time_slot: Time slot (interpreted as hour of day)

        Returns:
            Expected number of requests (float, noisy)
        """
        # Deterministic pattern
        hourly_multiplier = self._get_hourly_pattern(time_slot)
        mean_load = self.base_load * hourly_multiplier

        # Add Gaussian noise
        noise = random.gauss(0, self.noise_stddev * mean_load)
        noisy_load = mean_load + noise

        # Ensure non-negative
        return max(0.0, noisy_load)

    def predict_requests(
        self,
        start_slot: int,
        end_slot: int,
        deadline_slack_range: tuple = (1, 5)
    ) -> List[PredictedRequest]:
        """
        Generate predicted requests for time window.

        Each slot generates predict_load(slot) requests with random deadlines
        in [arrival + min_slack, arrival + max_slack].

        Args:
            start_slot: Start of window
            end_slot: End of window (inclusive)
            deadline_slack_range: (min_slack, max_slack) for deadline generation

        Returns:
            List of PredictedRequest objects
        """
        predicted = []

        for t in range(start_slot, end_slot + 1):
            # Number of requests arriving at slot t
            n_requests = int(round(self.predict_load(t)))

            for i in range(n_requests):
                # Random deadline slack
                min_slack, max_slack = deadline_slack_range
                slack = random.randint(min_slack, max_slack)

                req = PredictedRequest(
                    id=f"pred_{t}_{i}",
                    arrival_time=t,
                    deadline=t + slack,
                    estimated=True
                )
                predicted.append(req)

        return predicted

    def get_daily_pattern(self) -> List[float]:
        """
        Get deterministic 24-hour pattern (no noise).

        Useful for visualization and testing.

        Returns:
            List of 24 load values (one per hour)
        """
        return [
            self.base_load * self._get_hourly_pattern(hour)
            for hour in range(24)
        ]


class HistoricalRequestPredictor(RequestPredictor):
    """
    Predictor based on historical data (stub for future ML models).

    Could use:
    - Time series models (ARIMA, Prophet, LSTM)
    - Learning from past workload traces
    - Seasonal patterns (weekday vs weekend)

    Currently returns simple moving average as placeholder.
    """

    def __init__(self, historical_data: Dict[int, float]):
        """
        Initialize with historical load data.

        Args:
            historical_data: {time_slot: actual_load} mapping
        """
        self.historical_data = historical_data
        self.window_size = 3  # Moving average window

    def predict_load(self, time_slot: int) -> float:
        """Simple moving average prediction"""
        # Get past window_size slots
        past_slots = range(
            max(0, time_slot - self.window_size),
            time_slot
        )

        past_loads = [
            self.historical_data.get(t, 0.0)
            for t in past_slots
        ]

        if past_loads:
            return sum(past_loads) / len(past_loads)
        else:
            return 0.0

    def predict_requests(self, start_slot: int, end_slot: int) -> List[PredictedRequest]:
        """Placeholder: use moving average for each slot"""
        predicted = []

        for t in range(start_slot, end_slot + 1):
            n_requests = int(round(self.predict_load(t)))

            for i in range(n_requests):
                req = PredictedRequest(
                    id=f"hist_{t}_{i}",
                    arrival_time=t,
                    deadline=t + random.randint(1, 5),
                    estimated=True
                )
                predicted.append(req)

        return predicted
