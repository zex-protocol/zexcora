import time
import copy
import json
import multiprocessing
from secp256k1 import PublicKey

monitor_pub = '033452c6fa7b1ac52c14bb4ed4b592ffafdae5f2dba7f360435fd9c71428029c71'
monitor_pub = PublicKey(bytes.fromhex(monitor_pub), raw=True)

def __verify(txs):
    from zex import DEPOSIT, WITHDRAW, BUY, SELL, CANCEL
    res = [None] * len(txs)
    ts = 0
    # print(time.time())
    for i, tx in enumerate(txs):
        name = tx[1]
        if name == DEPOSIT:
            msg = tx[:-(8+64)]
            sig = tx[-(8+64):-8]
            verified = monitor_pub.schnorr_verify(msg, sig, bip340tag='zex')
        else:
            t = time.time()
            if name == CANCEL:
                msg, pubkey, sig = tx[:74], tx[41:74], tx[74:74+64]
            else:
                msg, pubkey, sig = tx[:73], tx[40:73], tx[73:73+64]
            pubkey = PublicKey(pubkey, raw=True)
            sig = pubkey.ecdsa_deserialize_compact(sig)
            verified = pubkey.ecdsa_verify(msg, sig)
            ts += time.time() - t
        if not verified:
            print('not verified', msg)
        res[i] = verified
    # print('chunk verification time:', ts)
    return res

def chunkify(lst, n_chunks):
    for i in range(0, len(lst), n_chunks):
        yield lst[i:i + n_chunks]

def verify(txs):
    t = time.time()
    n_chunks = multiprocessing.cpu_count()
    chunks = list(chunkify(txs, len(txs) // n_chunks + 1))
    with multiprocessing.Pool(processes=n_chunks) as pool:
        results = pool.map(__verify, chunks)

    i = 0
    for sublist in results:
        for verified in sublist:
            if not verified:
                txs[i] = None
            i += 1
    print('verification time:', time.time() - t)


if __name__ == '__main__':
    print(list(chunkify([1, 2, 3, 4, 5, 6], 2)))