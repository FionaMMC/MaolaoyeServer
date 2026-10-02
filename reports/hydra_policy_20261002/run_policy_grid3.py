"""Follow-up grid 3: escalate the sell guard instead of a fixed width.

First sell attempt keeps the tight guard (harvests short-term reversal seen in
grid 2); later attempts widen so de-risking still completes inside the window.
"""
from policy_replay import Policy
import run_policy_grid2 as grid2

C1 = dict(grid2.C1)
POLICIES = [
    Policy('P0_legacy'),
    Policy('C1', **C1),
    Policy('C2', **grid2.C2),
    Policy('C3_50_200', **C1, sell_schedule=(50., 200.)),
    Policy('C3_50_50_200', **C1, sell_schedule=(50., 50., 200.)),
    Policy('C3_50_100_200', **C1, sell_schedule=(50., 100., 200.)),
    Policy('C3_100_300', **C1, sell_schedule=(100., 300.)),
    Policy('C3_50_200_replan', **C1, sell_schedule=(50., 200.), sizing='replan'),
    Policy('C3_50_200_w5', **dict(C1, window=5), sell_schedule=(50., 200.)),
]
STRESS = ['P0_legacy', 'C1', 'C2', 'C3_50_200', 'C3_50_50_200']

if __name__ == '__main__':
    grid2.run_grid(POLICIES, STRESS, 'g3', 'summary3.json')
