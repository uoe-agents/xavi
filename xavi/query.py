from dataclasses import dataclass, field
from enum import Enum
from typing import Tuple, Dict, List

import logging
import igp2 as ip
import numpy as np

from xavi.matching import ActionMatching, ActionSegment

logger = logging.getLogger(__name__)


class QueryType(Enum):
    """ Supported query types. """
    WHY = "why"
    WHY_NOT = "whynot"
    WHAT_IF = "whatif"
    WHAT = "what"


@dataclass
class Query:
    """ Dataclass to store parsed query information.

    Args:
        type: The type of the query. Either why, whynot, whatif, or what.
        t_query: The time the user asked the query.
        action: The action the user is interested in.
        agent_id: The specific ID of the agent, the user queried.
        negative: If true then the query will be matched against all trajectories that are NOT equal to action.
        t_action: The start timestep of the action in question.
        tau: The number of timesteps to rollback from the present for counterfactual generation.
        tense: Past, future, or present. Indicates the time of the query.
    """

    type: QueryType
    t_query: int = None
    action: str = None
    agent_id: int = None
    negative: bool = None
    t_action: int = None
    tau: int = None
    tense: str = None

    fps: int = 20
    tau_limits: np.ndarray = np.array([1, 5])

    def __post_init__(self):
        self.__matching = ActionMatching()

        # Perform value checks
        self.type = QueryType(self.type)
        assert self.action in self.__matching.action_library, f"Unknown action {self.action}."
        assert self.tense in ["past", "present", "future", None], f"Unknown tense {self.tense}."

    def get_tau(self,
                current_t: int,
                observations: Dict[int, Tuple[ip.StateTrajectory, ip.AgentState]],
                rollouts: ip.AllMCTSResult):
        """ Calculate tau and the start time step of the queried action.
        Storing results in fields tau, and t_action.

        Args:
            current_t: The current timestep of the simulation
            observations: Trajectories observed (and possibly extended with future predictions) of the environment.
            rollouts: The actual MCTS rollouts of the agent.
        """
        agent_id = self.agent_id
        if self.type == QueryType.WHAT_IF:
            agent_id = self.agent_id

        # Obtain relevant trajectory slice and segment it
        trajectory = observations[agent_id][0]
        if self.tense in ["past", "present"]:
            trajectory = trajectory.slice(0, current_t)
        elif self.tense == "future":
            trajectory = trajectory.slice(current_t, None)
        elif self.tense is None:
            logger.warning(f"Query time was not given. Falling back to observed trajectory.")
            trajectory = trajectory.slice(0, current_t)
        len_states = len(trajectory)
        action_segmentations = self.__matching.action_segmentation(trajectory)

        tau = len_states - 1
        if self.type == QueryType.WHY:
            t_action, tau = self.__get_tau_factual(action_segmentations, True)
        elif self.type == QueryType.WHY_NOT:
            t_action, tau = self.__get_tau_counterfactual(action_segmentations, len_states, True)
        elif self.type == QueryType.WHAT_IF:
            if self.negative:
                t_action, _ = self.__get_tau_factual(action_segmentations, False)
            else:
                t_action, _ = self.__get_tau_counterfactual(action_segmentations, len_states, False)
        elif self.type == QueryType.WHAT:
            # TODO (mid): Assumes query parsing can extract reference time point.
            t_action = self.t_action
        else:
            raise ValueError(f"Unknown query type {self.type}.")

        assert tau is not None and tau >= 0, f"Tau cannot be None or negative."
        if tau == 0:
            logger.warning(f"Rollback to the start of an entire observation.")

        if self.tau is None:
            self.tau = tau  # If user gave fixed tau then we shouldn't override that.
        self.t_action = t_action

    def __get_tau_factual(self,
                          action_segmentations: List[ActionSegment],
                          rollback: bool) -> (int, int):
        """ determine t_action for final causes, tau for efficient cause for why and whatif negative question.
        Args:
            action_segmentations: the segmented action of the observed trajectory.
            rollback: if rollback is needed

        Returns:
            t_action: the timestep when the factual action starts
            tau: the timesteps to rollback
        """
        tau = -1
        action_matched = False
        n_segments = len(action_segmentations)
        for i, action in enumerate(action_segmentations[::-1]):
            if self.action in action.actions:
                action_matched = True
            elif action_matched:
                t_action = action.times[-1] + 1
                segment_inx = i
                break
        else:
            if action_matched:
                t_action = 1
                segment_inx = n_segments - 1
            else:
                raise ValueError(f"Could not match action {self.action} to trajectory.")

        if rollback and segment_inx >= 0:
            # In case one extra segment is too short, then lower limit is used.
            lower_limit, upper_limit = self.tau_limits * self.fps
            previous_inx = max(0, n_segments - segment_inx - 1)
            previous_segment = action_segmentations[previous_inx]
            tau = previous_segment.times[0]

            if t_action - tau < lower_limit:
                tau = int(t_action - lower_limit)
            elif t_action - tau > upper_limit:
                tau = int(t_action - upper_limit)

            tau = max(1, tau)

        return t_action, tau

    def __get_tau_counterfactual(self,
                                 action_segmentations: List[ActionSegment],
                                 len_states: int,
                                 rollback: bool) -> (int, int):
        """ determine t_action for final causes, tau for efficient cause for whynot and whatif positive question.
        Args:
            action_segmentations: the segmented action of the observed trajectory.
            len_states: the length of observation
            rollback: if rollback is needed

        Returns:
            t_action: the timestep when the factual action starts
            tau: the timesteps to rollback
        """
        counter_actions = self.__matching.find_counter_actions(self.action)
        matched_action = None
        action_matched = False
        seg_inx = -1
        t_action = -1
        tau = -1
        for i, action in enumerate(action_segmentations[::-1]):
            if not matched_action:
                for action_ in action.actions:
                    if action_ not in counter_actions:
                        continue
                    action_matched = True
                    matched_action = action_
                    break
            if action_matched and matched_action not in action.actions:
                t_action = action.times[-1] + 1
                seg_inx = i
                break
        logger.info(f'factual action found is {matched_action}')
        if rollback and seg_inx >= 0:
            previous_segment = action_segmentations[len(action_segmentations) - seg_inx - 1]
            tau = len_states - previous_segment.times[0]

        return t_action, tau
