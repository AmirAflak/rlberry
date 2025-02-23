from collections import deque

import rlberry
import rlberry.spaces as spaces
from rlberry.envs.interface import Model

logger = rlberry.logger


class Bandit(Model):
    """
    Base class for a stochastic multi-armed bandit.

    Parameters
    ----------
    laws: list of laws.
        laws of the arms. can either be a frozen scipy law or any class that
        has a method .rvs().

    **kwargs: keywords arguments
        additional arguments sent to :class:`~rlberry.envs.interface.Model`

    Attributes
    ----------
    laws: list
        laws of the arms. can either be a frozen scipy law or any class that
        has a method .rvs().
    n_arms: int
        Number of arms.
    action_space: spaces.Discrete
        Action space when viewing the bandit as a single-state MDP.
    rewards: list
        For each arm, pre-sample 10 times.
    n_rewards: list
        Reward counter per arm.
    """

    name = ""

    def __init__(self, laws=[], **kwargs):
        Model.__init__(self, **kwargs)
        self.laws = laws
        self.n_arms = len(self.laws)
        self.action_space = spaces.Discrete(self.n_arms)

        # Pre-sample 10 samples
        self.rewards = [
            deque(self.laws[action].rvs(size=10, random_state=self.rng))
            for action in range(self.n_arms)
        ]
        self.n_rewards = [10] * self.n_arms

    def step(self, action):
        """
        Sample the reward associated to the action.
        """
        # test that the action exists
        assert action < self.n_arms

        reward = self.laws[action].rvs(random_state=self.rng, size=1)[0]
        terminated = True
        truncated = False

        return 0, reward, terminated, truncated, {}

    def reset(self, seed=None, option=None):
        """
        Reset the environment to a default state.
        """
        return 0, {}


class AdversarialBandit(Model):
    """
    Base class for a adversarial multi-armed bandit with oblivious
    opponent, i.e all rewards are fixed in advance at the start of the run.

    Parameters
    ----------
    rewards: list of rewards, shape (T, A).
        Possible rewards up to horizon T for each of the A arms.

    **kwargs: keywords arguments
        additional arguments sent to :class:`~rlberry.envs.interface.Model`

    """

    name = ""

    def __init__(self, rewards=[], **kwargs):
        Model.__init__(self, **kwargs)
        self.n_arms = rewards.shape[1]
        self.rewards = deque(rewards)
        self.action_space = spaces.Discrete(self.n_arms)

    def step(self, action):
        """
        Sample the reward associated to the action.
        """
        # test that the action exists
        assert action < self.n_arms

        rewards = self.rewards.popleft()
        reward = rewards[action]
        terminated = True
        truncated = False
        return 0, reward, terminated, truncated, {}

    def reset(self, seed=None, option=None):
        """
        Reset the environment to a default state.
        """
        return 0, {}
