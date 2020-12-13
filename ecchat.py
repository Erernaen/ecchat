#!/usr/bin/env python3
# coding: UTF-8

import datetime
import settings
import argparse
import pathlib
import logging
import socket
import pycurl
import signal
import codecs
import urwid
import time
import json
import zmq
import sys
import re

from itertools import count

# ZMQ event loop adapter for urwid

from zmqeventloop import zmqEventLoop

# Full node RPC interface

from slickrpc import Proxy
from slickrpc import exc

eccoin = Proxy('http://%s:%s@%s' % (settings.rpc_user, settings.rpc_pass, settings.rpc_address))

################################################################################
## urwid related ###############################################################
################################################################################

class GridFlowPlus(urwid.GridFlow):

	def keypress(self, size, key):

		if isinstance(key, str):

			if key in ('tab', ):

				if self.focus_position == len(self.contents) - 1:

					self.focus_position = 0

				else:

					self.focus_position += 1

				return

			if key in ('esc', 'N', 'n'):

				self.focus_position = 1

				return super().keypress(size, 'enter')

			if key in ('Y', 'y'):

				self.focus_position = 0

				return super().keypress(size, 'enter')

		return super().keypress(size, key)

################################################################################

class YesNoDialog(urwid.WidgetWrap):

	signals = ['commit']

	def __init__(self, text, loop):

		self.loop = loop

		self.parent = self.loop.widget

		self.body = urwid.Filler(urwid.Text(text))

		self.frame = urwid.Frame(self.body, focus_part = 'body')

		self.view = urwid.Padding(self.frame, ('fixed left', 2), ('fixed right' , 2))
		self.view = urwid.Filler (self.view,  ('fixed top' , 1), ('fixed bottom', 1))
		self.view = urwid.LineBox(self.view)
		self.view = urwid.Overlay(self.view, self.parent, 'center', len(text) + 6, 'middle', 7)

		self.frame.footer = GridFlowPlus([urwid.AttrMap(urwid.Button('Yes', self.on_yes), 'btn_nm', 'btn_hl'),
			                              urwid.AttrMap(urwid.Button('No' , self.on_no) , 'btn_nm', 'btn_hl')],
			                             7, 3, 1, 'center')

		self.frame.focus_position = 'footer'

		super().__init__(self.view)

	############################################################################

	def on_yes(self, *args, **kwargs):

		self.loop.widget = self.parent

		urwid.emit_signal(self, 'commit')

	############################################################################

	def on_no(self, *args, **kwargs):

		self.loop.widget = self.parent

	############################################################################

	def show(self):

		self.loop.widget = self.view

################################################################################

class MessageListBox(urwid.ListBox):

	def __init__(self, body):

		super().__init__(body)

	############################################################################

	def render(self, size, *args, **kwargs):

		self.last_render_size = size

		return super().render(size, *args, **kwargs)

	############################################################################

	def key(self, key):

		#TODO - check scrolling keypresses and pass back to footer edit control

		super().keypress(self.last_render_size, key)

################################################################################
## eccPacket class #############################################################
################################################################################

class eccPacket():

	TYPE_chatMsg = 'chatMsg'
	TYPE_addrReq = 'addrReq'
	TYPE_addrRes = 'addrRes'
	TYPE_txidInf = 'txidInf'

	def __init__(self, _id = '', _ver = '', _to = '', _from = '', _type = '', _data = ''):

		# TOTO: Add some validation checks here

		self.packet = {	'id'	: _id,
						'ver'	: _ver,
						'to'	: _to,
						'from'	: _from,
						'type'	: _type,
						'data'	: _data}

	############################################################################

	@classmethod

	def from_json(cls, json_string = ''):

		d = json.loads(json_string)

		# TOTO: Add some validation checks here

		return cls(d['id'], d['ver'], d['to'], d['from'], d['type'], d['data'])

	############################################################################

	def get_from(self):

		return self.packet['from']

	############################################################################

	def get_type(self):

		return self.packet['type']

	############################################################################

	def get_data(self):

		return self.packet['data']

	############################################################################

	def send(self):

		eccoin.sendpacket(self.packet['to'], self.packet['id'], json.dumps(self.packet))

################################################################################
## ChatApp class ###############################################################
################################################################################

class ChatApp:

	_bufferIdx = count(start=1)

	_clock_fmt = '[%H:%M:%S] '

	_palette = [
				('header', 'black'           , 'brown'      , 'standout'),
				('status', 'black'           , 'brown'      , 'standout'),
				('text'  , 'light gray'      , 'black'      , 'default' ),
				('time'  , 'brown'           , 'black'      , 'default' ),
				('self'  , 'light cyan'      , 'black'      , 'default' ),
				('other' , 'light green'     , 'black'      , 'default' ),
				('ecchat', 'brown'           , 'black'      , 'default' ),
				('scroll', 'text'                                       ),
				('footer', 'text'                                       ),
				('btn_nm', 'black'           , 'brown'      , 'default' ),
				('btn_hl', 'black'           , 'yellow'     , 'standout')]

	def __init__(self, name, tag):

		urwid.set_encoding('utf-8')

		self.version = '1.1'

		self.party_name = ['ecchat', name, '[other]']

		self.party_separator  = ['|', '>', '<']

		self.party_name_style = ['ecchat', 'self', 'other']

		self.party_text_style = ['ecchat', 'text', 'text']

		self.party_size = max(len(t) for t in self.party_name)

		self.otherTag = tag

		self.TxID = ''

	############################################################################

	def build_ui(self):

		self.walker = urwid.SimpleListWalker([])

		self.headerT = urwid.Text    (u'ecchat {} : {} > {}'.format(self.version, self.party_name[1], self.party_name[2]))
		self.headerA = urwid.AttrMap (self.headerT, 'header')

		self.scrollT = MessageListBox(self.walker)
		self.scrollA = urwid.AttrMap (self.scrollT, 'scroll')

		self.statusT = urwid.Text    (u'Initializing ...')
		self.statusA = urwid.AttrMap (self.statusT, 'status')

		self.footerT = urwid.Edit    ('> ')
		self.footerA = urwid.AttrMap (self.footerT, 'footer')

		self.frame  = urwid.Frame(body = self.scrollA, header = self.headerA, footer = self.statusA)

		self.window = urwid.Frame(body = self.frame, footer = self.footerA, focus_part = 'footer')

	############################################################################

	def append_message(self, party, text):

		self.walker.append(urwid.Text([('time', time.strftime(self._clock_fmt)), (self.party_name_style[party], u'{0:>{1}s} {2} '.format(self.party_name[party], self.party_size, self.party_separator[party])), (self.party_text_style[party], text)]))

		self.scrollT.set_focus(len(self.scrollT.body) - 1)

	############################################################################

	def clock_refresh(self, loop = None, data = None):

		self.statusT.set_text(time.strftime(self._clock_fmt))

		loop.set_alarm_in(1, self.clock_refresh)

	############################################################################

	def process_user_entry(self, text):

		if len(text) > 0:

			if text.startswith('/exit'):

				self.check_quit('exit')

			elif text.startswith('/quit'):

				self.check_quit('quit')

			elif text.startswith('/help'):

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				self.append_message(0, '%-8s - %s' % ('/help', 'display help information'))
				self.append_message(0, '%-8s - %s' % ('/exit', 'exit - also /quit and ESC'))
				self.append_message(0, '%-8s - %s' % ('/version', 'display ecchat version info'))
				self.append_message(0, '%-8s - %s' % ('/blocks', 'display eccoin block count'))
				self.append_message(0, '%-8s - %s' % ('/balance', 'display $ECC wallet balance'))
				self.append_message(0, '%-8s - %s' % ('/send x', 'send $ECC x to other party'))
				self.append_message(0, '%-8s - %s' % ('/txid', 'display TxID of last send'))

			elif text.startswith('/version'):

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				self.append_message(0, self.version)

			elif text.startswith('/blocks'):

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				blocks = eccoin.getblockcount()

				self.append_message(0, '{:d}'.format(blocks))

			elif text.startswith('/balance'):

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				balance = eccoin.getbalance()

				self.append_message(0, '{:f}'.format(balance))

			elif text.startswith('/send'):

				match = re.match('/send ', text)

				amount = text[match.end():]

				self.TxID = 'b6046cf6223ad5f5d9f5656ed428b7cc14d007f334105e693a0e2d1699d2dc92'

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				self.append_message(0, '$ECC %s sent ' % amount)

			elif text.startswith('/txid'):

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				if self.TxID:

					self.append_message(0, 'TxID = %s' % self.TxID)

				else:

					self.append_message(0, 'TxID = %s' % 'none')

			else:

				self.footerT.set_edit_text(u'')

				self.append_message(1, text)

				ecc_packet = eccPacket(settings.protocol_id, settings.protocol_ver, self.otherTag, self.selfTag, eccPacket.TYPE_chatMsg, text)

				ecc_packet.send()

	############################################################################

	def unhandled_keypress(self, key):

		if isinstance(key, str):

			if key in ('up', 'down', 'page up', 'page down'):

				self.scrollT.key(key)

			if key in ('enter', ):

				self.process_user_entry(self.footerT.get_edit_text())

			if key in ('esc', ):

				self.check_quit()

	############################################################################

	def check_quit(self, command = 'quit'):

		self.footerT.set_edit_text(u'')

		dialog = YesNoDialog(text = u'Do you want to %s ?' % command, loop = self.loop)

		urwid.connect_signal(dialog, 'commit', self.quit)

		dialog.show()

	############################################################################

	def quit(self, *args, **kwargs):

		raise urwid.ExitMainLoop()

	############################################################################

	def zmqInitialise(self):

		self.event_loop = zmqEventLoop()
	
		self.context    = zmq.Context()
	
		self.subscriber = self.context.socket(zmq.SUB)
	
		self.subscriber.connect('tcp://%s'%settings.zmq_address)
	
		self.subscriber.setsockopt(zmq.SUBSCRIBE, b'')
	
		self.event_loop.watch_queue(self.subscriber, self.zmqHandler, zmq.POLLIN)

	############################################################################

	def zmqShutdown(self):

		self.subscriber.close()

		self.context.term()

	############################################################################

	def zmqHandler(self):

		[address, contents] = self.subscriber.recv_multipart(zmq.DONTWAIT)
		
		if address.decode() == 'packet':

			protocolID = contents.decode()[1:]

			bufferCmd = 'GetBufferRequest:' + protocolID + str(next(self._bufferIdx))

			bufferSig = eccoin.buffersignmessage(self.bufferKey, bufferCmd)

			eccbuffer = eccoin.getbuffer(int(protocolID), bufferSig)

			for packet in eccbuffer.values():

				message = codecs.decode(packet, 'hex').decode()

				ecc_packet = eccPacket.from_json(message)

				if   ecc_packet.get_type() == eccPacket.TYPE_chatMsg:

					self.append_message(2, ecc_packet.get_data())

				elif ecc_packet.get_type() == eccPacket.TYPE_addrReq:

					pass

				elif ecc_packet.get_type() == eccPacket.TYPE_addrReq:

					pass

				else:

					pass

	############################################################################

	def eccoinInitialise(self):

		self.bufferKey = ""

		try:

			self.selfTag = eccoin.getroutingpubkey()

		except pycurl.error:

			print('Failed to connect - check that local eccoin daemon is running')

			return False

		except exc.RpcInWarmUp:

			print('Failed to connect - local eccoin daemon is starting but not ready - try again after 60 seconds')

			return False

		try:

			self.bufferKey = eccoin.registerbuffer(settings.protocol_id)

		except exc.RpcInternalError:

			print('API Buffer was not correctly unregistered previously - restart local eccoin daemon to fix')

			return False

		try:

			eccoin.findroute(self.otherTag)

			isRoute = eccoin.haveroute(self.otherTag)

		except exc.RpcInvalidAddressOrKey:

			print('Routing tag has invalid base64 encoding : %s' % self.otherTag)

			return False

		if not isRoute:

			print('No route available to : %s' % self.otherTag)

		return isRoute

	############################################################################

	def eccoinShutdown(self):

		if self.bufferKey:

			bufferSig = eccoin.buffersignmessage(self.bufferKey, 'ReleaseBufferRequest')

			eccoin.releasebuffer(settings.protocol_id, bufferSig)

	############################################################################

	def run(self):

		if self.eccoinInitialise():

			self.zmqInitialise()

			self.build_ui()

			self.loop = urwid.MainLoop(widget          = self.window,
			                           palette         = self._palette,
			                           handle_mouse    = True,
			                           unhandled_input = self.unhandled_keypress,
			                           event_loop      = self.event_loop)

			self.loop.set_alarm_in(1, self.clock_refresh)

			self.loop.run()

			self.zmqShutdown()

		self.eccoinShutdown()

################################################################################

def terminate(signalNumber, frame):

	logging.info('%s received - terminating' % signal.Signals(signalNumber).name)

	raise urwid.ExitMainLoop()

################################################################################
### Main program ###############################################################
################################################################################

def main():

	if sys.version_info[0] < 3:

		raise 'Use Python 3'

	pathlib.Path('log').mkdir(parents=True, exist_ok=True)

	logging.basicConfig(filename = 'log/{:%Y-%m-%d}.log'.format(datetime.datetime.now()),
						filemode = 'a',
						level    = logging.INFO,
						format   = '%(asctime)s - %(levelname)s : %(message)s',
						datefmt  = '%d/%m/%Y %H:%M:%S')

	logging.info('STARTUP')

	signal.signal(signal.SIGINT,  terminate)  # keyboard interrupt ^C
	signal.signal(signal.SIGTERM, terminate)  # kill [default -15]

	argparser = argparse.ArgumentParser(description='Simple command line chat for ECC')

	argparser.add_argument('-n', '--name', action='store', help='nickname    (local)' , type=str, default = '', required=True)
	argparser.add_argument('-t', '--tag' , action='store', help='routing tag (remote)', type=str, default = '', required=True)

	command_line_args = argparser.parse_args()

	logging.info('Arguments %s', vars(command_line_args))

	app = ChatApp(command_line_args.name, command_line_args.tag)

	app.run()

	logging.info('SHUTDOWN')

################################################################################

if __name__ == '__main__':

	main()

################################################################################
