from tqdm import tqdm
import argparse
import json
import time
import numpy as np
import os
from models.char_lstm import CharLstm
from models.char_translator import CharTranslator
from collections import defaultdict
from utils.data_provider import DataProvider
from utils.utils import repackage_hidden
from torch.autograd import Variable, Function

import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence
import math

def adv_forward_pass(modelGen, modelEval, inps, lens, end_c=0, maxlen=100, auths=None,
        cycle_compute=True, append_symb=None):
    modelGen.eval()
    modelEval.eval()
    b_sz = len(lens)

    gen_samples, gen_lens, char_outs = modelGen.forward_advers_gen(inps, lens, end_c=end_c, n_max=maxlen, auths=auths)

    len_sorted, gen_lensort_idx = gen_lens.sort(dim=0, descending=True)
    _, rev_sort_idx = gen_lensort_idx.sort(dim=0)

    eval_inp = torch.cat([torch.unsqueeze(c,0) for c in char_outs])
    # Apply gradient filtering
    eval_inp = eval_inp.index_select( 1, gen_lensort_idx)

    #--------------------------------------------------------------------------
    # The output need to be sorted by length to be fed into further LSTM stages
    #--------------------------------------------------------------------------
    eval_out_gen = modelEval.forward_classify(eval_inp, lens=len_sorted.tolist(), compute_softmax=True)
    # Undo the sorting here
    eval_out_gen= eval_out_gen[0].data.index_select(0, rev_sort_idx)
    #---------------------------------------------------
    # Now pass the generated samples to the evaluator
    # output has format: [auth_classifier out, hidden state, generic classifier out (optional])
    #---------------------------------------------------
    if cycle_compute:
        reverse_inp = torch.cat([append_symb.repeat(1,b_sz), eval_inp],dim=0)
        #reverse_inp = reverse_inp.detach()
        _, rev_gen_lens, rev_char_outs = modelGen.forward_advers_gen(reverse_inp, len_sorted.tolist(), end_c=end_c, n_max=maxlen, auths=1-auths)
        rev_char_outs = [rc.index_select(0,rev_sort_idx) for rc in rev_char_outs]
        samples_out = (char_outs, gen_lens, rev_char_outs, rev_gen_lens)
    else:
        samples_out = (char_outs, gen_lens)

    return (eval_out_gen,) + samples_out

#def adv_eval_pass(modelGen, modelEval, inps, lens, end_c=0, maxlen=100, auths=None):
#
#    char_outs = modelGen.forward_gen(inps, end_c=end_c, n_max=maxlen, auths=auths)
#    #--------------------------------------------------------------------------
#    # The output need to be sorted by length to be fed into further LSTM stages
#    #--------------------------------------------------------------------------
#    gen_len = len(char_outs)
#    eval_inp = torch.unsqueeze(torch.cat(char_outs),1).data
#    if (gen_len <= 0):
#        import ipdb
#        ipdb.set_trace()
#
#    #---------------------------------------------------
#    # Now pass the generated samples to the evaluator
#    # output has format: [auth_classifier out, hidden state, generic classifier out (optional])
#    #---------------------------------------------------
#    eval_out_gen = modelEval.forward_classify(eval_inp, lens=[gen_len], compute_softmax=True)
#    # Undo the sorting here
#    samples_out = (gen_len, char_outs)
#
#    return eval_out_gen + samples_out

def main(params):

    # Create vocabulary and author index
    saved_model = torch.load(params['genmodel'])
    cp_params = saved_model['arch']
    if params['evalmodel']:
        eval_model = torch.load(params['evalmodel'])
        eval_params = eval_model['arch']
        eval_state = eval_model['state_dict']
    else:
        print "FIX THIS"
        return

    if 'misc' in saved_model:
        misc = saved_model['misc']
        char_to_ix = misc['char_to_ix']
        auth_to_ix = misc['auth_to_ix']
        ix_to_char = misc['ix_to_char']
        ix_to_auth = misc['ix_to_auth']
    else:
        char_to_ix = saved_model['char_to_ix']
        auth_to_ix = saved_model['auth_to_ix']
        ix_to_char = saved_model['ix_to_char']
        ix_to_auth = saved_model['ix_to_auth']

    dp = DataProvider(cp_params)
    modelGen = CharTranslator(cp_params)
    modelEval = CharLstm(eval_params)

    startc = dp.data['configs']['start']
    endc = dp.data['configs']['end']

    modelGen.eval()
    modelEval.eval()

    # Restore saved checkpoint
    modelGen.load_state_dict(saved_model['state_dict'])
    state = modelEval.state_dict()
    state.update(eval_state)
    modelEval.load_state_dict(state)

    append_tensor = np.zeros((1, 1), dtype=np.int)
    append_tensor[0, 0] = char_to_ix[startc]
    append_tensor = torch.LongTensor(append_tensor).cuda()

    accum_diff_eval = [[],[]]
    accum_err_eval = np.zeros(len(auth_to_ix))
    accum_err_real = np.zeros(len(auth_to_ix))
    accum_count_gen = np.zeros(len(auth_to_ix))


    accum_recall_forward = np.zeros(len(auth_to_ix))
    accum_prec_forward = np.zeros(len(auth_to_ix))
    accum_recall_rev = np.zeros(len(auth_to_ix))
    accum_prec_rev = np.zeros(len(auth_to_ix))

    jc = '' if cp_params.get('atoms','char') == 'char' else ' '
    result = {'docs':[], 'misc':None, 'cp_params':cp_params, 'params': params}
    c_doc = {'sents':[]}

    for i, b_data in tqdm(enumerate(dp.iter_sentences(split=params['split'], atoms=cp_params.get('atoms','word'), batch_size = params['batch_size']))):
        if i > params['num_samples'] and params['num_samples']>0:
            break;
    #for i in xrange(params['num_samples']):
        #c_aid = np.random.choice(auth_to_ix.values())
        #batch = dp.get_sentence_batch(1,split=params['split'], atoms=cp_params.get('atoms','char'), aid=ix_to_auth[c_aid])
        c_bsz = len(b_data[0])
        done = b_data[1]
        inps, targs, auths, lens = dp.prepare_data(b_data[0], char_to_ix, auth_to_ix, maxlen=cp_params['max_seq_len'])
        # outs are organized as
        auths_inp = 1 - auths if params['flip'] else auths
        outs = adv_forward_pass(modelGen, modelEval, inps, lens,
                end_c=char_to_ix[endc], maxlen=cp_params['max_seq_len'],
                auths=auths_inp, cycle_compute=params['show_rev'],
                append_symb=append_tensor)

        eval_out_gt = modelEval.forward_classify(targs, lens=lens, compute_softmax=True)
        real_aid_out = eval_out_gt[0].data[:, auths_inp[0]]

        gen_aid_out = outs[0][:, auths_inp[0]]
        accum_err_eval[auths_inp[0]] += (gen_aid_out>= 0.5).float().sum()
        accum_err_real[auths_inp[0]] += (real_aid_out>= 0.5).float().sum()
        accum_count_gen[auths_inp[0]] += c_bsz
        accum_diff_eval[auths_inp[0]].append(gen_aid_out[0] - real_aid_out[0])

        for b in xrange(inps.size()[1]):
            inpset =  set(inps[:,b].tolist()[:lens[b]]) ; genset = set([c[b] for c in outs[1][:outs[2][b]]]);
            accum_recall_forward[auths_inp[b]] += (float(len(genset & inpset)) / float(len(inpset)))
            accum_prec_forward[auths_inp[b]] += (float(len(genset & inpset)) / float(len(genset)))

            if params['show_rev']:
                revgenset = set([c[b] for c in outs[-2][:outs[-1][b]] ])
                accum_recall_rev[auths_inp[b]]  += (float(len(revgenset & inpset)) / float(len(inpset)))
                accum_prec_rev[auths_inp[b]]    += (float(len(revgenset & inpset)) / float(len(revgenset)))
        for b in xrange(inps.size()[1]):
            inp_text = jc.join([ix_to_char[c] for c in targs[:,b] if c in ix_to_char])
            trans_text = jc.join([ix_to_char[c.cpu()[b]] for c in outs[1][:outs[2][b]] if c.cpu()[b] in ix_to_char])
            c_doc['sents'].append({'sent':inp_text,'score':eval_out_gt[0][b].data.cpu().tolist(), 'trans': trans_text, 'trans_score':outs[0][b].cpu().tolist()})
        if done:
            c_doc['attrib'] = b_data[0][-1]['attrib']
            result['docs'].append(c_doc)
            c_doc = {'sents':[]}

        if params['print']:
            print '--------------------------------------------'
            print 'Author: %s'%(b_data[0][0]['author'])
            print 'Inp text %s: %s (%.2f)'%(ix_to_auth[auths[0]], jc.join([ix_to_char[c[0]] for c in inps[1:]]), real_aid_out[0])
            print 'Out text %s: %s (%.2f)'%(ix_to_auth[auths_inp[0]],jc.join([ix_to_char[c.cpu()[0]] for c in outs[1] if c.cpu()[0] in ix_to_char]), gen_aid_out[0])
            if params['show_rev']:
                print 'Rev text %s: '%(ix_to_auth[auths[0]])+ '%s'%(jc.join([ix_to_char[c.cpu()[0]] for c in outs[-2] if c.cpu()[0] in ix_to_char]))
        #else:
        #    print '%d/%d\r'%(i, params['num_samples']),

    err_a1, err_a2 = accum_err_eval[0]/(1e-5+accum_count_gen[0]), accum_err_eval[1]/(1e-5+accum_count_gen[1])
    err_real_a1, err_real_a2 = accum_err_real[0]/(1e-5+accum_count_gen[0]), accum_err_real[1]/(1e-5+accum_count_gen[1])
    print '--------------------------------------------'
    print 'Efficiency in fooling discriminator'
    print '--------------------------------------------'
    print(' erra1 {:3.2f} - erra2 {:3.2f}'.format(100.*err_a1, 100.*err_a2))
    print(' err_real_a1 {:3.2f} - err_real_a2 {:3.2f}'.format(100.*err_real_a1, 100.*err_real_a2))
    print(' count %d - %d'%(accum_count_gen[0], accum_count_gen[1]))

    diff_arr0, diff_arr1 =  np.array(accum_diff_eval[0]), np.array(accum_diff_eval[1])
    print 'Mean difference : translation to %s = %.2f , translation to %s = %.2f '%(ix_to_auth[0], diff_arr0.mean(), ix_to_auth[1], diff_arr1.mean())
    print 'Difference > 0  : translation to %s = %.2f%%, translation to %s = %.2f%% '%(ix_to_auth[0], 100.*(diff_arr0>0).sum()/(1e-5+diff_arr0.shape[0]), ix_to_auth[1], 100.*(diff_arr1>0).sum()/(1e-5+diff_arr1.shape[0]))

    print '\n--------------------------------------------'
    print 'Consistencey with the input text'
    print '--------------------------------------------'
    print 'Generated text A0- Precision = %.2f, Recall = %.2f'%(accum_prec_forward[0]/accum_count_gen[0], accum_recall_forward[0]/accum_count_gen[0] )
    print 'Generated text A1- Precision = %.2f, Recall = %.2f'%(accum_prec_forward[1]/accum_count_gen[1], accum_recall_forward[1]/accum_count_gen[1] )
    if params['show_rev']:
        print '\n'
        print 'Reconstr  text A0- Precision = %.2f, Recall = %.2f'%(accum_prec_rev[0]/accum_count_gen[0], accum_recall_rev[0]/accum_count_gen[0] )
        print 'Reconstr  text A1- Precision = %.2f, Recall = %.2f'%(accum_prec_rev[1]/accum_count_gen[1], accum_recall_rev[1]/accum_count_gen[1] )

    if params['dumpjson']:
       json.dump(result, open(params['dumpjson'],'w'))


if __name__ == "__main__":

  parser = argparse.ArgumentParser()
  parser.add_argument('-g','--genmodel', dest='genmodel', type=str, default=None, help='generator/GAN checkpoint filename')
  parser.add_argument('-e','--evalmodel', dest='evalmodel', type=str, default=None, help='evakcheckpoint filename')
  parser.add_argument('-s','--split', dest='split', type=str, default='val', help='which split to evaluate')
  parser.add_argument('-b','--batch_size', dest='batch_size', type=int, default=1, help='batch_size to use')
  parser.add_argument('--num_samples', dest='num_samples', type=int, default=0, help='how many strings to generate')
  parser.add_argument('--show_rev', dest='show_rev', type=int, default=0, help='how many strings to generate')
  parser.add_argument('-l','--max_len', dest='max_len', type=int, default=100, help='how many characters to generate per string')
  parser.add_argument('--seed_length', dest='seed_length', type=int, default=100, help='character length of seed to the generator')
  parser.add_argument('-i', '--interactive', dest='interactive', action='store_true', help='Should it be interactive ')
  parser.add_argument('--m_type', dest='m_type', type=str, default='generative', help='type')
  parser.add_argument('--flip', dest='flip', type=int, default=0, help='flip authors')
  parser.add_argument('--print', dest='print', type=int, default=0, help='Print scores')
  parser.add_argument('--dumpjson', dest='dumpjson', type=str, default=None, help='Print scores')


  args = parser.parse_args()
  params = vars(args) # convert to ordinary dict
  main(params)

