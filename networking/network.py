#!/usr/bin/python
# Original Author : https://github.com/benediktkr at /ncpoc
# Modified by CVSC

from datetime import datetime
from functools import partial
from twisted.internet import reactor
from twisted.internet.protocol import Protocol, Factory
from twisted.internet.endpoints import TCP4ClientEndpoint
from twisted.internet.endpoints import connectProtocol
from twisted.internet.task import LoopingCall
from twisted.internet.error import CannotListenError
from twisted.internet.endpoints import TCP4ServerEndpoint, TCP4ClientEndpoint
import messages
import cryptotools
import os, inspect, sys
import ConfigParser



currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0,parentdir)

from collections import OrderedDict
from dboperations import *
from bootstrap_nodes import *
import version 
from main import *
from time import time
import pickle




PING_INTERVAL = 1200.0 # 20 min = 1200.0
SYNC_INTERVAL = 15 # 15 seconds


def _print(*args):
    time = datetime.now().time().isoformat()[:8]
    print time,
    print " ".join(map(str, args))

class NCProtocol(Protocol):
    def __init__(self, factory, state="GETHELLO", kind="LISTENER"):
        self.factory = factory
        self.state = state
        self.VERSION = 0
        self.ProtocolVersion = version._protocol_version
        self.remote_nodeid = None
        self.remote_node_protocol_version = None
        self.kind = kind
        self.nodeid = self.factory.nodeid



        self.lc_ping = LoopingCall(self.send_PING)
        self.lc_sync = LoopingCall(self.send_SYNC)
        self.message = partial(messages.envelope_decorator, self.nodeid)
        
        self.factory.status = "Running"

    def connectionMade(self):
        r_ip = self.transport.getPeer()
        h_ip = self.transport.getHost()
        self.remote_ip = r_ip.host + ":" + str(r_ip.port)
        self.host_ip = h_ip.host + ":" + str(h_ip.port)

    def print_peers(self):
        if len(self.factory.peers) == 0:
            logg(" [!] PEERS: No peers connected.")
        else:
            logg(" [ ] PEERS:")
            for peer in self.factory.peers:
                addr, kind = self.factory.peers[peer][:2]
                logg(" [*] %s at %s %s " %(peer, addr, kind))

    def write(self, line):
        self.transport.write(line + "\n")

    def connectionLost(self, reason):
        # NOTE: It looks like the NCProtocol instance will linger in memory
        # since ping keeps going if we don't .stop() it.
        try: self.lc_ping.stop()
        except AssertionError: pass

        try:
            self.factory.peers.pop(self.remote_nodeid)
            if self.nodeid != self.remote_nodeid:
                self.print_peers()
        except KeyError:
            if self.nodeid != self.remote_nodeid:
                _print(" [ ] GHOST LEAVES: from", self.remote_nodeid, self.remote_ip)

    def dataReceived(self, data):
        for line in data.splitlines():
            line = line.strip()
            envelope = messages.read_envelope(line)
            if self.state in ["GETHELLO", "SENTHELLO"]:
                # Force first message to be HELLO or crash
                if envelope['msgtype'] == 'hello':
                    self.handle_HELLO(line)
                else:
                    logg(" [!] Ignoring", envelope['msgtype'], "in", self.state)
            else:
                if envelope['msgtype'] == 'ping':
                    self.handle_PING(line)
                elif envelope['msgtype'] == 'pong':
                    self.handle_PONG(line)
                elif envelope['msgtype'] == 'addr':
                    self.handle_ADDR(line)
                elif envelope['msgtype'] == 'sync':
                    self.handle_SYNC(line)
                elif envelope['msgtype'] == 'givemeblocks':
                    self.handle_SENDBLOCKS(line)
                elif envelope['msgtype'] == 'getblock':
                    self.handleRECEIVEDBLOCK(line)

    def send_PING(self):
        logg(" [>] PING   to %s %s" %(self.remote_nodeid, self.remote_ip))
        ping = messages.create_ping(self.nodeid)
        self.write(ping)



    def handle_PING(self, ping):
        if messages.read_message(ping):
            pong = messages.create_pong(self.nodeid)
            self.write(pong)



    def send_SYNC(self):
        logg("[>] Asking %s if we need sync" %self.remote_nodeid)
        # Send a sync message to remote peer 
        # A sync message contains our best height and our besthash
        sync = messages.create_sync(self.nodeid, CBlockchain().getBestHeight(), CBlockchain().GetBestHash())
        self.write(sync)



    def handle_SYNC(self, line):
        logg("[>] Got reply about sync message from %s" %self.remote_nodeid)
        data = messages.read_message(line)

        if data["bestheight"] > CBlockchain().getBestHeight():
            # we have missing blocks
            diffrence = data["bestheight"] - CBlockchain().getBestHeight()
            logg("We need sync, we are behind %d blocks" %diffrence)
            self.factory.dialog = "Need sync"
            syncme = messages.create_ask_blocks(self.nodeid, CBlockchain().GetBestHash())
            self.write(syncme)

        elif data["bestheight"] == CBlockchain().getBestHeight():
            self.factory.dialog = "Synced"
            logg("we are synced")



    def handle_SENDBLOCKS(self, line):
        logg("[>] Got sendblocks message from %s" %self.remote_nodeid)
        data = messages.read_message(line)
        try:
            thisHeight = CBlockIndex(data["besthash"]).Height()
        except Exception as e:
            self.transport.loseConnection()
        else:
            # be sure that we are not behind, and peer has genesis block 
            if thisHeight < CBlockchain().getBestHeight() and thisHeight >=1:
                data_block, pblock, nonce = CaBlock(thisHeight +1).dump()
                cblock = pickle.dumps(pblock)  
                cdatablock = pickle.dumps(data_block)
                message = messages.create_send_block(self.nodeid, cdatablock, cblock, nonce)
                self.write(message)
                logg("block %d send to %s" %(thisHeight +1, self.remote_nodeid))



    def handleRECEIVEDBLOCK(self, line):
        data = messages.read_message(line)
        logg("Proccesing block %d from %s" %(CBlockchain().getBestHeight() +1, self.remote_nodeid))
        data_block = pickle.loads(data["raw"])
        pblock = pickle.loads(data["pblock"])
        nonce = data["bnonce"]
        
        # transactions are hashed via dict, 
        # when a dict changes order produces diffrent tx hash 
        # ordering to avoid diffrent hashes 
        for x in xrange(len(pblock.vtx)):
            r = OrderedDict(pblock.vtx[x])
            pblock.vtx[x] = dict(r.items())

        # procces this block 
        if Proccess().thisBlock(data_block, pblock, nonce):
            logg("Block accepted\n")
            if CBlockchain().WriteBlock(pblock, data_block, nonce):
                logg("Block successfull added to database")


    def send_ADDR(self):
        logg(" [>] Telling to %s about my peers" %self.remote_nodeid)
        # Shouldn't this be a list and not a dict?
        peers = self.factory.peers
        listeners = [(n, peers[n][0], peers[n][1], peers[n][2])
                     for n in peers]
        addr = messages.create_addr(self.nodeid, listeners)
        self.write(addr)



    def handle_ADDR(self, addr):
        try:
            nodes = messages.read_message(addr)['nodes']
            logg(" [<] Recieved addr list from peer %s" %self.remote_nodeid)
            #for node in filter(lambda n: nodes[n][1] == "SEND", nodes):
            for node in nodes:
                logg(" [*] %s %s" %(node[0], node[1]))

                if node[0] == self.nodeid:
                    logg("[!] Not connecting to %s thats me!" %node[0])
                    return
                if node[1] != "SPEAKER":
                    logg("[!] Not connecting to %s is %s" %(node[0], node[1]))
                    return
                if node[0] in self.factory.peers:
                    logg("[!] Not connecting to %s already connected" %node[0])
                    return
                _print(" [ ] Trying to connect to peer " + node[0] + " " + node[1])
                # TODO: Use [2] and a time limit to not connect to "old" peers
                host, port = node[0].split(":")
                point = TCP4ClientEndpoint(reactor, host, int(port))
                d = connectProtocol(point, NCProtocol(ncfactory, "SENDHELLO", "SPEAKER"))
                d.addCallback(gotProtocol)
        except messages.InvalidSignatureError:
            print addr
            _print(" [!] ERROR: Invalid addr sign ", self.remote_ip)
            self.transport.loseConnection()



    def handle_PONG(self, pong):
        pong = messages.read_message(pong)
        logg("[<] PONG from %s at %s" %(self.remote_nodeid, self.remote_ip))
        # hacky
        addr, kind = self.factory.peers[self.remote_nodeid][:2]
        self.factory.peers[self.remote_nodeid] = (addr, kind, time())



    def send_HELLO(self):
        hello = messages.create_hello(self.nodeid, self.VERSION, self.ProtocolVersion)
        #_print(" [ ] SEND_HELLO:", self.nodeid, "to", self.remote_ip)
        self.transport.write(hello + "\n")
        self.state = "SENTHELLO"



    def handle_HELLO(self, hello):
        try:
            hello = messages.read_message(hello)
            self.remote_nodeid = hello['nodeid']
            self.remote_node_protocol_version = hello["protocol"]


            if self.remote_nodeid == self.nodeid:
                logg("[!] Found myself at %s" %self.host_ip)
                self.transport.loseConnection()
            else:
                if self.state == "GETHELLO":
                    my_hello = messages.create_hello(self.nodeid, self.VERSION, self.ProtocolVersion)
                    self.transport.write(my_hello + "\n")
                self.add_peer()
                self.state = "READY"
                self.print_peers()
                #self.write(messages.create_ping(self.nodeid))
                if self.kind == "LISTENER":
                    # The listener pings it's audience
                    logg("[ ] Starting pinger to %s" %self.remote_ip)
                    self.lc_ping.start(PING_INTERVAL, now=False)
                    # Tell new audience about my peers
                    self.send_ADDR()
                self.lc_sync.start(SYNC_INTERVAL, now=True)
        except messages.InvalidSignatureError:
            _print(" [!] ERROR: Invalid hello sign ", self.remote_ip)
            self.transport.loseConnection()



    def add_peer(self):
        entry = (self.remote_ip, self.kind, self.remote_node_protocol_version, time())
        self.factory.peers[self.remote_nodeid] = entry
        logg("[] peer %s at %s with protocol version %d added to peers list" %(self.remote_nodeid, self.remote_ip, self.remote_node_protocol_version))



# Splitinto NCRecvFactory and NCSendFactory (also reconsider the names...:/)
class NCFactory(Factory):
    def __init__(self):
        self.peers = {}
        self.numProtocols = 0
        self.nodeid = cryptotools.generate_nodeid()[:10]
        self.status = None
        self.dialog = "n/a"

    def startFactory(self):
        logg("Node started")

    def stopFactory(self):
        reactor.callFromThread(reactor.stop)

    def buildProtocol(self, addr):
        return NCProtocol(self, "GETHELLO", "LISTENER")

def gotProtocol(p):
    # ClientFactory instead?
    p.send_HELLO()
    
    
def Start(factory):

    config = ConfigParser.ConfigParser()
    config.read(GetAppDir() + "/silme.conf")
    
    p2p_host = config.get('p2p', 'host')
    p2p_port = config.get('p2p', 'port')
        
    try:
        endpoint = TCP4ServerEndpoint(reactor, int(p2p_port), interface=p2p_host)
        logg(" [ ] LISTEN: at %s:%d" %(p2p_host, (int(p2p_port))))
        endpoint.listen(factory)
    except CannotListenError:
        logg("[!] Address in use")
        raise SystemExit


    # connect to bootstrap addresses
    logg(" [ ] Trying to connect to bootstrap hosts:")
    for bootstrap in BOOTSTRAP_NODES:
        logg("     [*] %s" %bootstrap)
        host, port = bootstrap.split(":")
        point = TCP4ClientEndpoint(reactor, host, int(port))
        d = connectProtocol(point, NCProtocol(factory, "SENDHELLO", "LISTENER"))
        d.addCallback(gotProtocol)
    
    reactor.run(installSignalHandlers=0)
