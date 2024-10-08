import pandas as pd
import torch
import logging
import sys
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

sys.path.append('../evaluation/')

df_list = []
for arm in range(49):
    df_list.append(pd.read_csv('../data/data/imdb/bao/plans/job_ext_arm{}.csv'.format(arm)))

import pickle

with open('../data/data/imdb/bao/plans/bao_dat.pkl', 'rb') as inp:
    dat = pickle.load(inp)
planss = dat['planss']
latencies = dat['latencies']
rootss = dat['rootss']

# Print the first root (assuming rootss is a list of lists)
# print("First root in rootss:", rootss[0])

# Print a specific element of a specific root
# print("First element of the first root:", rootss[0][0])
del dat

from dataset_utils import *

all_roots = sum(rootss, [])
ds_info = DatasetInfo({})
ds_info.construct_from_plans(all_roots)

minmax = pd.read_csv('../data/data/imdb/column_min_max_vals.csv')
col_min_max = get_col_min_max(minmax)
ds_info.get_columns(col_min_max)

import random


class BanditOptimizer():
    def __init__(self, planss, rootss, latencies, look_back=800, N=100, freq=100):
        print("Initializing BanditOptimizer")
        ## system settings
        self.N = N
        self.look_back = look_back
        self.freq = freq
        ##

        self.planss = planss
        self.rootss = rootss
        self.latencies = latencies

        self.arms = len(self.latencies)
        self.total = len(self.latencies[0])
        self.cur_query = freq
        self.selections = [0 for i in range(freq)]
        self.tm = [0 for i in range(freq)]  # inference
        self.tl = [0 for i in range(freq)]  # pre-process
        self.tr = [0 for i in range(freq)]  # train
        self.exe_time = []
        ## record results

        random.seed(42)
        self.sample_ids = []
        for i in range(0, self.total // self.freq + 1):
            left = max(0, i * self.freq - self.look_back)
            right = (i + 1) * self.freq
            ids = random.choices(range(left, right), k=self.N)
            self.sample_ids.append(ids)
        self.spl = 0

        print("BanditOptimizer initialized")

    def get_execution_time(self):
        print("Calculating execution time")
        exe_time = []
        for i, sel in enumerate(self.selections):
            exe_time[i] = self.latencies[i][sel]
        self.exe_time = exe_time
        print("Execution time calculated: %s", exe_time)
        return exe_time

    def initial_data(self):
        print("Fetching initial data")
        return self.rootss[0][:self.freq], self.latencies[0][:self.freq]

    def sample_data(self):
        print("Sampling data")
        if self.cur_query == self.freq:
            return self.initial_data()

        if self.spl >= len(self.sample_ids):
            logging.warning('Should already be done, please check')
            return None
        sample_ids = self.sample_ids[self.spl]
        self.spl += 1

        roots = []
        lats = []
        for idx in sample_ids:
            sel = self.selections[idx]
            roots.append(self.rootss[sel][idx])
            lats.append(self.latencies[sel][idx])

        print("Sampled data - Roots: %s, Latencies: %s", roots[:1],
              lats[:1])  # Print first elements as sample output
        return roots, lats

    def select_plans(self, model, get_batch):
        print("Selecting plans")
        sels = []
        right = min(self.total, self.cur_query + self.freq)
        qids = range(self.cur_query, right)
        tm = []
        tl = []
        for qid in qids:
            roots = [self.rootss[i][qid] for i in range(self.arms)]
            lats = [self.latencies[i][qid] for i in range(self.arms)]

            t0 = time.time()
            batch = get_batch(roots, lats)
            # Print the batch (this will show tensor details if it's a tensor)
            # print("Batch generated by get_batch:", batch)

            # If the batch is a tensor, you can print specific elements or its shape
            # if isinstance(batch, torch.Tensor):
                # print("Shape of batch tensor:", batch.shape)
                # print("First element in the batch tensor:", batch[0])

            t1 = time.time()
            out = model(batch).squeeze()
            t2 = time.time()
            tm.append(t2 - t1)
            tl.append(t1 - t0)

            sels.append(out.detach().cpu().argmin().numpy().item())

            del batch

        self.selections += sels
        self.cur_query = right
        self.tm += tm
        self.tl += tl
        print('Model Time: %s, Preprocessing Time: %s', sum(tm), sum(tl))

        latss = [[self.latencies[i][qid] for i in range(self.arms)] for qid in qids]
        best_lats = 0
        post_lats = 0
        sel_lats = 0
        for i, qid in enumerate(qids):
            lats = [self.latencies[k][qid] for k in range(self.arms)]
            post_lats += self.latencies[0][qid] / 1000
            best_lats += min(lats) / 1000
            sel_lats += self.latencies[sels[i]][qid] / 1000
        print('Best Time: %s, Post Time: %s, Sel Time: %s', best_lats, post_lats, sel_lats)

        return self.selections

    def train_time(self, tr):
        print("Recording training time")
        self.tr.append(tr)
        remain_len = min(self.freq - 1, self.total - len(self.tr))
        self.tr += [0 for a in range(remain_len)]
        print("Training time recorded: %s", tr)


def get_custom(latencies, df):
    total_lats = []
    execution_lats = []
    for i, row in df.iterrows():
        sel = row['Selections']
        lat = latencies[sel][i] / 1000
        execution_lats.append(lat)
        total = lat + row['Train Time'] + \
                row['Inf Time'] + row['Preprocess Time']
        total_lats.append(total)
    print("Custom latencies calculated")
    return total_lats, execution_lats


from algorithms.avgdl import AVGDL_Dataset, AVGDL
from algorithms.avgdl import Encoding as avgdl_Encoding
from algorithms.avgdl import DataLoader as avgdl_loader
from algorithms.avgdl import collate as avgdl_collate


class Args:
    device = 'cuda:1'
    # device = 'cpu'
    bs = 128
    epochs = 200
    lr = 1e-3
    hid = 64
    save_path = 'results/bao/avgdl/'


args = Args()
import os

save_path = args.save_path
if not os.path.exists(save_path):
    os.makedirs(save_path)

seed = 0
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = True

from trainer import Prediction, train
from torch import nn

encoding = avgdl_Encoding()
avgdl = AVGDL(32, 64, 64)
prediction = Prediction(64, args.hid)
model = nn.Sequential(avgdl, prediction)
_ = model.to(args.device)


def construct_loader(ds_info):
    def get_loader(roots, costs):
        _train_roots = roots
        ds = AVGDL_Dataset(_train_roots, encoding, costs, ds_info)
        return avgdl_loader(ds, batch_size=len(roots), collate_fn=avgdl_collate, shuffle=False)

    return get_loader


def construct_batch(get_loader, args):
    def get_batch(roots, costs):
        loader = get_loader(roots, costs)
        return next(iter(loader))[0].to(args.device)

    return get_batch


get_loader = construct_loader(ds_info)
get_batch = construct_batch(get_loader, args)

N = 400
look_back = 800
freq = 100
bo_agent = BanditOptimizer(planss, rootss, latencies, look_back=look_back, N=N, freq=freq)

for steps in range(len(latencies[0]) // freq):
    print("Step %d", steps)
    t0 = time.time()
    dat = bo_agent.sample_data()
    if dat:
        loader = get_loader(*dat)
        train(model, loader, loader, dat[1], ds_info, args, prints=False, record=False)
        bo_agent.train_time(time.time() - t0)
        print('Training Time: %s', time.time() - t0)
        bo_agent.select_plans(model, get_batch)

res = df_list[0].copy()
del res['json']
res['Train Time'] = bo_agent.tr
res['Inf Time'] = bo_agent.tm
res['Preprocess Time'] = bo_agent.tl
res['Selections'] = bo_agent.selections

# Saving the results DataFrame as a CSV file
csv_file_path = 'results/bao/avgdl/optimization_results.csv'
res.to_csv(csv_file_path, index=False)
print(f"Results have been saved to {csv_file_path}")


arms = len(latencies)
length = len(latencies[0])
best_sels = []
best_lats = []
worst_lats = []

for i in range(length):
    lats = [latencies[k][i] for k in range(arms)]
    mini = min(lats)
    best_lats.append(mini)
    best_sels.append(lats.index(mini))
    worst_lats.append(max(lats))

queries_complete = range(length)

best = np.cumsum(best_lats) / 1000 / 60
post = np.cumsum(latencies[0]) / 1000 / 60

print("Processing complete")
