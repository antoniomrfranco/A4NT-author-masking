from tqdm import tqdm
import argparse
import json
import time
import numpy as np
import os
from models.char_lstm import CharLstm
import torch
import torch.nn as nn
from utils.data_provider import DataProvider


def main(params):

    eval_model = torch.load(params['evalmodel'])
    eval_params = eval_model['arch']
    eval_state = eval_model['state_dict']
    modelEval = CharLstm(eval_params)

    char_to_ix = eval_model['char_to_ix']
    auth_to_ix = eval_model['auth_to_ix']
    ix_to_char = eval_model['ix_to_char']

    dp = DataProvider(eval_params)
    modelEval.eval()
    state = modelEval.state_dict()
    state.update(eval_state)
    modelEval.load_state_dict(state)

    inps = json.load(open(params['inpfile'],'r'))
    bsz = 100

    def process_batch(batch, featstr = 'sent_enc'):
        _, targs, _,lens = dp.prepare_data(batch, char_to_ix, auth_to_ix, maxlen=eval_params['max_seq_len'])
        if not all(lens):
            import ipdb; ipdb.set_trace()
        eval_out = modelEval.forward_classify(targs, lens=lens,compute_softmax=True)
        eval_out = eval_out[0].data.cpu().numpy()
        for i,b in enumerate(batch):
            inps['docs'][b['id']]['sents'][b['sid']][b['sampid']][featstr] = eval_out[i,:].tolist()


    batch = []
    for i,doc in tqdm(enumerate(inps['docs'])):
        for j, st in enumerate(doc['sents']):
            for k in xrange(len(st)):
                st = inps['docs'][i]['sents'][j][k]['trans'].split()
                if len(st) > 0:
                    batch.append({'in': st,'targ': st, 'author': inps['docs'][i]['author'],
                        'id':i, 'sid': j, 'sampid':k})
                if len(batch) == bsz:
                    process_batch(batch, featstr = 'trans_score')
                    del batch
                    batch = []
    if batch:
        process_batch(batch, featstr = 'trans_score')
        del batch
        batch = []
    json.dump(inps, open(params['inpfile'],'w'))

if __name__ == "__main__":

  parser = argparse.ArgumentParser()
  parser.add_argument('-e','--evalmodel', dest='evalmodel', type=str, default=None, help='evakcheckpoint filename')
  parser.add_argument('-i','--inpfile', dest='inpfile', type=str, default=None, help='evakcheckpoint filename')

  args = parser.parse_args()
  params = vars(args) # convert to ordinary dict
  main(params)
