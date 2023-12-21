import sys
import time
import json
import logging
import asyncio
import traceback
from os import getenv
from uuid import uuid4
from web3.auto import Web3
from datetime import datetime
from dotenv import load_dotenv
from websockets import connect
from flashbots import flashbot
from web3.exceptions import TransactionNotFound

load_dotenv()

logging.basicConfig(stream=sys.stderr, level=getenv("LOG_LEVEL"))
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("web3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

web3 = Web3(Web3.WebsocketProvider(getenv("PROVIDER_WSS")))
wallet = web3.eth.account.from_key(getenv("PRIV_KEY"))
flashbot(web3, web3.eth.account.from_key("fdae6f24fffa89ec2f936932b752a05edf75c18ef12d6351d41a5f0e144d7198"), getenv("FLASHBOTS_URL"))

if not web3.is_connected():
    exit("Connection fails")

with open('router_abi.json') as abi_file:
  router_abi = abi_file.read()
with open('factory_abi.json') as abi_file:
  factory_abi = abi_file.read()
with open('pair_abi.json') as abi_file:
  pair_abi = abi_file.read()
with open('erc20_abi.json') as abi_file:
  erc20_abi = abi_file.read()

router_contract = web3.eth.contract(address=web3.to_checksum_address(getenv("ROUTER_ADDRESS")), abi=json.loads(router_abi))
factory_address = router_contract.functions.factory().call()
factory_contract = web3.eth.contract(address=web3.to_checksum_address(factory_address), abi=json.loads(factory_abi))

def log_trx(trx, weth_addr, token_addr, pooled_eth):
    logging.debug(trx["hash"])
    logging.debug("WETH addr: " + weth_addr)
    logging.debug("Token addr: " + token_addr)
    logging.debug("ETH sent: {:.18f}".format(web3.from_wei(int(trx["value"], 16), "Ether")))
    logging.debug("Pooled ETH: {:.18f}".format(pooled_eth))
    logging.debug("{:.2f}% of total pool".format(float(web3.from_wei(int(trx["value"], 16), "Ether")) * 100 / pooled_eth))

def send_bundle(bundle):
    while True:
        block = web3.eth.block_number
        logging.debug("Simulating on block " + str(block))

        try:
            web3.flashbots.simulate(bundle, block)
            logging.debug("Simulation successful.")
        except Exception as e:
            logging.error("Simulation error", e)
            return
        
        logging.debug(f"Sending bundle targeting block {block+1}")
        replacement_uuid = str(uuid4())
        
        send_result = web3.flashbots.send_bundle(
            bundle,
            target_block_number=block + 1,
            opts={"replacementUuid": replacement_uuid},
        )
        logging.debug("bundleHash", web3.to_hex(send_result.bundle_hash()))

        stats_v1 = web3.flashbots.get_bundle_stats(
            web3.toHex(send_result.bundle_hash()), block
        )
        logging.debug("bundleStats v1", stats_v1)

        stats_v2 = web3.flashbots.get_bundle_stats_v2(
            web3.toHex(send_result.bundle_hash()), block
        )
        logging.debug("bundleStats v2", stats_v2)

        send_result.wait()
        try:
            receipts = send_result.receipts()
            logging.debug(f"\nBundle was mined in block {receipts[0].blockNumber}\a")
            break
        except TransactionNotFound:
            logging.error(f"Bundle not found in block {block+1}")

            cancel_res = web3.flashbots.cancel_bundles(replacement_uuid)
            logging.error(f"canceled {cancel_res}")

def check_slippage(token1_addr, token2_addr, victim_sent_wei, victim_out_min):
    pair_address = factory_contract.functions.getPair(token1_addr, token2_addr).call()
    pair_contract = web3.eth.contract(address=web3.to_checksum_address(pair_address), abi=json.loads(pair_abi))
    reserves = pair_contract.functions.getReserves().call()

    if int(token1_addr, 16) < int(token2_addr, 16):
        pooled_wei = reserves[0]
        pooled_token = reserves[1]
    else:
        pooled_wei = reserves[1]
        pooled_token = reserves[0]

    my_out = router_contract.functions.getAmountOut(web3.to_wei(getenv("BUY_ETH_AMOUNT"), "Ether"), pooled_wei, pooled_token).call()
    victim_out = router_contract.functions.getAmountOut(int(victim_sent_wei, 16), pooled_wei+my_out, pooled_token-my_out).call()

    if(victim_out < victim_out_min):
        raise Exception("Victim slippage too low...")

    return (float(web3.from_wei(pooled_wei, "Ether")), my_out)
    

def evetn_handler(pending_tx): 
    victim_trx = json.loads(pending_tx)['params']['result']

    try:
        func, inputs = router_contract.decode_function_input(victim_trx['input'])
    except Exception as err:
        logging.error(err)
        logging.error(victim_trx)
        return
    
    if func.fn_name.startswith("swapETH") or func.fn_name.startswith("swapExactETH") and len(inputs["path"]) == 2:
            try:
                start_time = time.time()
                
                pooled_eth, my_out = check_slippage(inputs['path'][0], inputs['path'][1], victim_trx["value"], inputs["amountOutMin"])
                log_trx(victim_trx, inputs['path'][0], inputs['path'][1], pooled_eth)
                
                logging.debug("Elapsed time: " + str(time.time() - start_time))

                if web3.from_wei(int(victim_trx["value"], 16), "Ether") > (pooled_eth * int(getenv("POOL_THRESHDOL"))):
                    logging.debug("Good trx to perform attack!")

                    buy_trx = router_contract.functions.swapExactETHForTokens(my_out, [inputs['path'][0], inputs['path'][1]], wallet.address, int(time.time()) + 30).build_transaction({
                        'nonce': web3.eth.get_transaction_count(wallet.address),
                        'from': wallet.address,
                        'value': web3.to_wei(getenv("BUY_ETH_AMOUNT"), "Ether"),
                        'maxFeePerGas': int(victim_trx["maxFeePerGas"], 16) + web3.to_wei(getenv("BRIBE_GWEI"), "gwei") if "maxFeePerGas" in victim_trx else web3.to_wei(getenv("BRIBE_GWEI"), "gwei"),
                        'maxPriorityFeePerGas': int(victim_trx["maxPriorityFeePerGas"], 16) + web3.to_wei(getenv("BRIBE_GWEI"), "gwei") if "maxPriorityFeePerGas" in victim_trx else web3.to_wei(getenv("BRIBE_GWEI"), "gwei")
                    })

                    token_contract = web3.eth.contract(address=web3.to_checksum_address(inputs['path'][1]), abi=json.loads(erc20_abi))
                    approve_trx = token_contract.functions.approve(getenv("ROUTER_ADDRESS"), my_out).build_transaction({
                        'maxFeePerGas': int(victim_trx["maxFeePerGas"], 16) + web3.to_wei(getenv("BRIBE_GWEI"), "gwei") if "maxFeePerGas" in victim_trx else web3.to_wei(getenv("BRIBE_GWEI"), "gwei"),
                        'maxPriorityFeePerGas': int(victim_trx["maxPriorityFeePerGas"], 16) + web3.to_wei(getenv("BRIBE_GWEI"), "gwei") if "maxPriorityFeePerGas" in victim_trx else web3.to_wei(getenv("BRIBE_GWEI"), "gwei")
                    })

                    sell_trx = router_contract.functions.swapExactTokensForETH(my_out, 0, [inputs['path'][1], inputs['path'][0]], wallet.address, int(time.time()) + 30).build_transaction({
                        'maxFeePerGas': int(victim_trx["maxFeePerGas"], 16) + web3.to_wei(getenv("BRIBE_GWEI"), "gwei") if "maxFeePerGas" in victim_trx else web3.to_wei(getenv("BRIBE_GWEI"), "gwei"),
                        'maxPriorityFeePerGas': int(victim_trx["maxPriorityFeePerGas"], 16) + web3.to_wei(getenv("BRIBE_GWEI"), "gwei") if "maxPriorityFeePerGas" in victim_trx else web3.to_wei(getenv("BRIBE_GWEI"), "gwei")
                    })

                    signed_buy = web3.eth.account.sign_transaction(buy_trx, getenv("PRIV_KEY"))
                    signed_victim_trx = web3.eth.account.sign_transaction(victim_trx, "")
                    signed_approve = web3.eth.account.sign_transaction(approve_trx, getenv("PRIV_KEY"))
                    signed_sell = web3.eth.account.sign_transaction(sell_trx, getenv("PRIV_KEY"))

                    bundle = [
                        {"signed_transaction": signed_buy.rawTransaction},
                        {"signed_transaction": signed_victim_trx.rawTransaction},
                        {"signed_transaction": signed_approve.rawTransaction},
                        {"signed_transaction": signed_sell.rawTransaction}
                    ]

                    send_bundle(bundle)
                    
                    logging.debug("TRX SENT: total elapsed time: " + str(time.time() - start_time))         
                    logging.debug("Trx timestamp: " + datetime.now().strftime("%d/%m/%Y, %H:%M:%S") + "\n\n")

            except Exception as err:
                logging.error(err + "\n")

async def subscribe_pending_trx():
    async with connect(getenv("PROVIDER_WSS")) as ws:
        await ws.send('{"jsonrpc":"2.0","id": 2, "method": "eth_subscribe", "params": ["alchemy_pendingTransactions", {"toAddress": ["' + getenv("ROUTER_ADDRESS") + '"], "hashesOnly": false}]}')
    
        while True:
            try:
                pending_tx = await asyncio.wait_for(ws.recv(), timeout=None)
                evetn_handler(pending_tx)

            except KeyboardInterrupt:
                exit()
            except (KeyError, TransactionNotFound):
                pass
            except Exception:
                logging.error(traceback.format_exc())

if __name__ == "__main__":
    asyncio.run(subscribe_pending_trx())