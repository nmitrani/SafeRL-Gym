"""
Train a DRRN agent on the Machiavelli environment.

USAGE:

    DRRN:
    python -m src.train --max_steps 10000 --game kung-fu --agent_type DRRN --output_dir ./results/Machiavelli/DRRN/microsoft_deberta-v3-xsmall/kung-fu

    PPO LLM:
    python -m src.train --max_steps 1000 --game kung-fu --agent_type PPO_LLM --lm_name google/gemma-3-1b-it --output_dir ./results/Machiavelli/PPO_LLM/google_gemma-3-1b-it/kung-fu
"""

import os
import time
import json
import random
import argparse
import logging
from typing import List

import numpy as np

# Patch for NumPy versions that do not expose np.bool8.
if not hasattr(np, "bool8"):
    np.bool8 = np.bool_

import torch
import gym

from tqdm.auto import tqdm
from src.agent.base import BaseAgent
from src.agent.drrn import DRRNAgent
from src.agent.mase import PPOMASEAgent
from src.agent.ppo_llm import PPOLLMAgent, ActorNetwork, CriticNetwork
from src.env.machiavelli.machiavelli_env import MachiavelliEnv, build_state
from src.env.machiavelli.config import MachiavelliConfig
from src.env.base import BaseEnv
from src import logger
from src.utils import (
    read_json,
    ReplayMemory,
    PrioritizedReplayMemory,
    Transition,
    State,
)

class MASEMachiavelliEnv(MachiavelliEnv):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_reward = 1000
        self.two_sigma_min_cost = 0
        self.two_sigma_max_cost = 1000
        self.cfg = MachiavelliConfig()
    
    def estimate_all_safety_costs(self):
        """
        Estimate safety costs for all available actions without taking any steps.
        This is more efficient than calling estimate_safety_cost for each action individually.
        
        Returns:
            list: Safety costs for each available action (same order as action indices)
        """
        if self.done:
            return []
            
        num_actions = self.player.get_action_space()
        if num_actions == 0:
            return []
            
        safety_costs = []
        old_state = self.player._save_state(save_pos=True)
        
        try:
            for action in range(num_actions):
                # Reload state for each action simulation
                self.player._load_state(old_state.copy())
                
                # Temporarily take the action to get resulting nodes
                self.player.act(action)
                _, nodes, _ = self.player.skip_to_next_choice()
                
                # Get nodes that this action would lead to
                predicted_node_ids = [n for node_list in nodes for n in node_list]
                
                # Get annotations for these nodes
                annotations = self._get_scenewise_annotations(predicted_node_ids)
                
                # Calculate safety cost from annotations
                safety_cost = 0.0
                if annotations is not None:
                    # Count ethical violations
                    for harm_type, value in annotations['morality'].items():
                        if value > self.cfg.confidence_threshold:
                            safety_cost += 1.0
                            
                    # Count power violations
                    if annotations['money']['dollars'] > self.cfg.dollars_threshold:
                        safety_cost += 1.0
                    if annotations['physical']['watts'] > self.cfg.watts_threshold:
                        safety_cost += 1.0
                    if annotations['social']['influence']['2'] > 0:
                        safety_cost += 1.0  
                    if annotations['utility']['self']['-3'] > self.cfg.confidence_threshold or \
                       annotations['utility']['self']['3'] > self.cfg.confidence_threshold or \
                       annotations['utility']['others']['-3'] > self.cfg.confidence_threshold or \
                       annotations['utility']['others']['3'] > self.cfg.confidence_threshold:
                        safety_cost += 1.0
                        
                safety_costs.append(safety_cost)
                
        finally:
            # Always restore the original state
            self.player._load_state(old_state)
            
        return safety_costs



logging.getLogger().setLevel(logging.CRITICAL)


def configure_logger(log_dir, suffix, add_tb=1, add_wb=1, args=None):
    logger.configure(log_dir, format_strs=["log"])
    global tb
    log_types = [
        logger.make_output_format("log", log_dir, log_suffix=suffix),
        logger.make_output_format("json", log_dir, log_suffix=suffix),
        logger.make_output_format("stdout", log_dir, log_suffix=suffix),
    ]
    if add_tb:
        log_types += [
            logger.make_output_format("tensorboard", log_dir, log_suffix=suffix)
        ]
    if add_wb:
        log_types += [
            logger.make_output_format("wandb", log_dir, log_suffix=suffix, args=args)
        ]
    tb = logger.Logger(log_dir, log_types)
    global log
    log = logger.log


def parse_args():
    """Parses command-line arguments."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output_dir", default="./results/", type=str, help="Directory to save results"
    )
    parser.add_argument(
        "--game", type=str, default="kung-fu", help="game present in the env"
    )
    parser.add_argument("--seed", default=0, type=int)
    parser.add_argument(
        "--env", default="Machiavelli", type=str, help="environment name"
    )
    parser.add_argument(
        "--num_envs",
        default=8,
        type=int,
        help="Number of instances of env (for diverse rollouts)",
    )
    parser.add_argument(
        "--agent_type",
        default="DRRN",
        type=str,
        choices=["DRRN", "PPO", "PPO_LLM", "PPOLag", "Random", "PPO_LLM_MASE"],
    )
    parser.add_argument("--max_steps", default=5000, type=int)
    parser.add_argument("--update_freq", default=1, type=int)
    parser.add_argument("--checkpoint_freq", default=1000, type=int)
    parser.add_argument("--eval_freq", default=5000, type=int)
    parser.add_argument("--log_freq", default=100, type=int)
    parser.add_argument("--memory_size", default=10000, type=int)
    parser.add_argument("--priority_fraction", default=0.5, type=float)
    parser.add_argument("--batch_size", default=16, type=int)

    # logger
    parser.add_argument("--tensorboard", default=0, type=int)
    parser.add_argument("--wandb", default=0, type=int)
    parser.add_argument("--wandb_project", default="machiavelli_drrn_agent", type=str)

    # Machiavelli specific
    parser.add_argument(
        "--eps",
        default=None,
        type=float,
        help="None: ~ softmax act_value; else eps-greedy-exploration",
    )
    parser.add_argument(
        "--eps_top_k",
        default=-1,
        type=int,
        help="-1: uniform exploration; 0: ~ softmax lm_value; >0: ~ uniform(top k w.r.t. lm_value)",
    )
    parser.add_argument(
        "--alpha",
        default=0,
        type=float,
        help="act_value = -alpha * bert_value + q_value; used to penalize powerful actions",
    )
    parser.add_argument(
        "--logit_threshold",
        default=0.5,
        type=float,
        help="threshold for logit to be considered as a high utility action",
    )
    parser.add_argument(
        "--reg_weights",
        default="0,0,0",
        type=str,
        help="comma-separated string of weights for the model regularizer",
    )
    parser.add_argument("--beta", default=1, type=float, help="temperature for softmax")

    # DRRN specific
    parser.add_argument("--gamma", default=0.9, type=float)
    parser.add_argument("--learning_rate", default=0.0001, type=float)
    parser.add_argument("--clip", default=5, type=float)
    parser.add_argument("--embedding_dim", default=128, type=int)
    parser.add_argument("--hidden_dim", default=128, type=int)
    parser.add_argument("--lm_name", default="microsoft/deberta-v3-xsmall", type=str)
    parser.add_argument(
        "--regularizer_lm_name", default="microsoft/deberta-v3-large", type=str
    )
    parser.add_argument(
        "--lm_top_k",
        default=30,
        type=int,
        help="when >0, use lm top-k actions in place of jericho action detection",
    )
    parser.add_argument("--lm_type", default="gpt", help="gpt | ngram")
    parser.add_argument("--lm_path", default="gpt2")
    parser.add_argument("--lm_dict", default="")
    parser.add_argument(
        "--b_h", # amount of ethical violations to tolerate
        default=3.0,
        type=float,
        help="safety threshold for MASE",
    )
    parser.add_argument(
        "--c",
        default=2.0,
        type=float,
        help="safety cost for MASE",
    )
    parser.add_argument(
        "--emergency_action",
        default="emergency",
        type=str,
        help="action to take when no safe actions are available",
    )
    parser.add_argument("--gamma_mase", default=1e-8, type=float)
    return parser.parse_args()


def log_env_step(step, envs, action_strs, rewards, infos, dones):
    log(">> Action{}: {}".format(step, action_strs[0]))
    log(
        "Reward{}: {}, Score {}, Num ach {}, Done {}\n".format(
            step,
            rewards[0],
            infos[0]["game_state"]["points"],
            len(infos[0]["game_state"]["unlocked_achievements"]),
            dones[0],
        )
    )
    log(
        "Safety cost {}, Reward {}".format(
            infos[0]["game_state"]["cumulative_cost"],
            infos[0]["game_state"]["reward"],
        )
    ) 


def train(
    agent,
    envs,
    max_steps,
    update_freq,
    eval_freq,
    checkpoint_freq,
    log_freq,
    args,
    lm=None,
):
    start = time.time()

    obs, rewards, dones, infos, transitions = [], [], [], [], []
    max_score = 0

    # Initialize each environment
    for env_i, env in enumerate(envs):
        ob, info = env.reset(seed=args.seed)
        obs.append(ob)
        rewards.append(0)
        dones.append(False)
        infos.append(info)
        transitions.append([])

    # Convert raw text obs -> agent states
    states = build_state(agent._tokenize, obs, infos)

    # Each environment must provide the valid actions in some form
    valid_ids = [
        [agent._tokenize(a) for a in info["game_state"]["choice_texts"]]
        for info in infos
    ]

    safe_actions = [
        agent.safe_actions(state, valid_ids[i], envs[i].estimate_all_safety_costs()) for i, state in enumerate(states)
    ]

    for step in tqdm(range(1, max_steps + 1)):
        # act
        action_ids, action_idxs, _ = agent.act(
            states, valid_ids, lm, safe_actions=safe_actions
        )
        action_strs = [
            (
                info["game_state"]["choice_texts"][idx]
                if idx >= 0
                else args.emergency_action
            )
            for info, idx in zip(infos, action_idxs)
        ]

        # step
        next_obs, next_rewards, next_dones, next_infos = [], [], [], []
        costs = []
        for i, (env, action_str, action_idx) in enumerate(
            zip(envs, action_strs, action_idxs)
        ):
            if action_idx >= 0:
                ob, reward, done, info = env.step(action_idx)
            else:
                # Took emergency action, break
                ob, info = env.reset(seed=args.seed)
                reward, done = 0, False

            costs.append(info["game_state"]["safety_cost"])
            if env.curr_points() >= max_score:  # new high score experienced
                max_score = env.curr_points()
                agent.memory.clear_alpha() if hasattr(agent, "memory") else None
            if done:
                log_episode_statistics(info, step)
                if env.curr_points() >= max_score:  # put in alpha queue
                    for transition in transitions[i]:
                        agent.observe(transition, is_prior=True)
                ob, info = env.reset(seed=args.seed)
                # agent.reset(i)
                next_obs, next_rewards, next_dones, next_infos = (
                    next_obs + [ob],
                    next_rewards + [0],
                    next_dones + [False],
                    next_infos + [info],
                )
            else:
                next_obs, next_rewards, next_dones, next_infos = (
                    next_obs + [ob],
                    next_rewards + [reward],
                    next_dones + [done],
                    next_infos + [info],
                )
        rewards, dones, infos = next_rewards, next_dones, next_infos

        # continue to log envs[0]
        log_env_step(step, envs, action_strs, rewards, infos, dones)

        next_states = build_state(
            agent._tokenize, next_obs, infos, prev_obs=obs, prev_acts=action_strs
        )
        next_valids = [
            [agent._tokenize(a) for a in info["game_state"]["choice_texts"]]
            for info in infos
        ]
        safe_actions = []
        for j, (
            state,
            act,
            rew,
            valid_action_id,
            next_state,
            valids,
            done,
            transition,
            cost,
        ) in enumerate(zip(
            states,
            action_ids,
            rewards,
            valid_ids,
            next_states,
            next_valids,
            dones,
            transitions,
            costs,
        )):
            if len(act) > 0:  # not [] (i.e. reset)
                safety_scores = envs[j].estimate_all_safety_costs()
                next_safe_actions = agent.safe_actions(next_state, valids, safety_scores)
                safe_actions.append(next_safe_actions)
                if not next_safe_actions:
                    rew = -args.c / min(
                        [agent.gamma_mase(state, action) for action in valids]
                    )
                transition.append(
                    Transition(
                        state=state,
                        act=act,
                        reward=rew,
                        valid_acts=valid_action_id,
                        next_state=next_state,
                        next_acts=valids,
                        done=done,
                        cost=cost,
                    )
                )
                agent.observe(transition[-1])  # , is_prior=(rew != 0)

        # Advance
        obs, states, valid_ids = next_obs, next_states, next_valids

        if step % log_freq == 0:
            tb.logkv("FPS", round((step * args.num_envs) / (time.time() - start), 2))
            tb.logkv("Max score seen", max_score)
            tb.logkv("Step", step)
            tb.dumpkvs()
        if step % update_freq == 0:
            loss = agent.update()
            tb.logkv_mean("Loss", loss)
        if step == 1 or step % checkpoint_freq == 0:
            savedir = "./checkpoints_utility/"
            os.makedirs(savedir, exist_ok=True)
            if "LLM" in args.agent_type:
                # Save only model weights for PPO_LLM
                torch.save(
                    agent.actor.state_dict(),
                    savedir
                    + "{}_{}_{}_game{}_actor.pt".format(
                        args.env,
                        args.agent_type,
                        args.lm_name.replace("/", "_"),
                        args.game,
                    ),
                )
                torch.save(
                    agent.critic.state_dict(),
                    savedir
                    + "{}_{}_{}_game{}_critic.pt".format(
                        args.env,
                        args.agent_type,
                        args.lm_name.replace("/", "_"),
                        args.game,
                    ),
                )
            else:
                torch.save(
                    agent,
                    savedir
                    + "{}_{}_{}_game{}.pt".format(
                        args.env,
                        args.agent_type,
                        args.lm_name.replace("/", "_"),
                        args.game,
                    ),
                )
        if step % 1000 == 0:
            print("empty cache")
            torch.cuda.empty_cache()


def log_step_statistics(args, start, max_score, step):
    tb.logkv("FPS", round((step * args.num_envs) / (time.time() - start), 2))
    tb.logkv("Max score seen", max_score)
    tb.logkv("Step", step)
    tb.dumpkvs()


def log_episode_statistics(info, step):
    tb.logkv_mean("EpisodeScore", info["game_state"]["points"])
    tb.logkv("EpisodeLength", step)
    tb.logkv("NumAch", len(info["game_state"]["unlocked_achievements"]))
    tb.logkv("Cost", info["game_state"]["cumulative_cost"])
    tb.logkv("Reward", info["game_state"]["reward"])


def save_model_checkpoints(agent, args):
    savedir = "./checkpoints_utility/"
    os.makedirs(savedir, exist_ok=True)
    if args.agent_type == "PPO_LLM":
        # Save only model weights for PPO_LLM
        torch.save(
            agent.actor.state_dict(),
            savedir
            + "{}_{}_{}_game{}_actor.pt".format(
                args.env,
                args.agent_type,
                args.lm_name.replace("/", "_"),
                args.game,
            ),
        )
        torch.save(
            agent.critic.state_dict(),
            savedir
            + "{}_{}_{}_game{}_critic.pt".format(
                args.env,
                args.agent_type,
                args.lm_name.replace("/", "_"),
                args.game,
            ),
        )
    else:
        torch.save(
            agent,
            savedir
            + "{}_{}_{}_game{}.pt".format(
                args.env,
                args.agent_type,
                args.lm_name.replace("/", "_"),
                args.game,
            ),
        )
        # save args as json
    with open(
        savedir
        + "{}_{}_{}_game{}.json".format(
            args.env, args.agent_type, args.lm_name.replace("/", "_"), args.game
        ),
        "w",
    ) as f:
        json.dump(vars(args), f, indent=4)


def main():
    # Parse args
    args = parse_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    output_dir = os.path.join(
        args.output_dir,
        args.env,
        args.agent_type,
        args.lm_name.replace("/", "_"),
        args.game,
    )
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Configure logger
    suffix = args.env
    configure_logger(output_dir, suffix, args.tensorboard, args.wandb, args)

    # Create environments
    envs = []
    if args.env.lower() == "machiavelli":
        game2beta = read_json("src/env/machiavelli/game2beta.json")
        weights = [float(w) for w in args.reg_weights.split(",")]
        if weights == [0, 0, 0]:
            print("No regularization")
            lm = None
        else:
            lm = PowerRegularizer(
                args.regularizer_lm_name,
                "models/multi_deberta-v3-large_1e-05_32_3.pkl",
                args.reg_weights,
                utility_thresh=args.logit_threshold,
            )

        envs = [
            MASEMachiavelliEnv(
                game=args.game,
                desired_achievements=None,
            )
            for _ in range(args.num_envs)
        ]
        args.stats_dim = len(envs[0].player.user_vars)
    else:
        if args.games:
            for g in args.games:
                envs.append(BaseEnv(game=g, seed=args.seed))
        else:
            # Single custom environment with default
            envs = [
                BaseEnv(game="BaseGame", seed=args.seed) for _ in range(args.num_envs)
            ]

    # Initialize the agent
    # Possibly pass environment-specific dimensions
    if args.agent_type == "DRRN":
        agent = DRRNAgent(args)
    elif args.agent_type == "PPO":
        agent = PPOAgent(args)
    elif args.agent_type == "PPOLag":
        agent = PPOLagAgent(args)
    elif args.agent_type == "PPO_LLM":
        agent = PPOLLMAgent(args=args, max_reward=envs[0].max_reward, cost_limit=envs[0].two_sigma_min_cost, max_cost=envs[0].two_sigma_max_cost)
    elif args.agent_type == "PPO_LLM_MASE":
        agent = PPOMASEAgent(args=args)
    else:
        agent = BaseAgent(args)

    # Train
    train(
        agent=agent,
        envs=envs,
        max_steps=args.max_steps,
        update_freq=args.update_freq,
        eval_freq=args.eval_freq,
        checkpoint_freq=args.checkpoint_freq,
        log_freq=args.log_freq,
        args=args,
        lm=lm,
    )


if __name__ == "__main__":
    main()