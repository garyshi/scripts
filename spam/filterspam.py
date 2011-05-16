#!/usr/bin/python
# -*- coding: utf-8 -*-
import sys
import getopt
import binascii
import subprocess
import logging, logging.handlers
from StringIO import StringIO
import email.Header
import email.Utils
from email.FeedParser import FeedParser

def decode_header(value):
	value,charset = email.Header.decode_header(value)[0]
	if not value: return value
	if not charset: return value
	if charset == 'gb2312': charset = 'gbk'
	elif charset == 'x-gb2312': charset = 'gbk'
	elif charset == 'x-gbk': charset = 'gbk'
	return value.decode(charset, 'replace')

def str_addrs(addrs):
	L = []
	for name,addr in addrs:
		if name: s = '%s <%s>' % (name, addr)
		else: s = '<%s>' % addr
		L.append(s)
	return ', '.join(L)

class CheckingMessage(object):
	def __init__(self, sender, rcpts, f):
		self.sender = sender
		self.rcpts = rcpts
		self.f = f
		self.hbuf = StringIO()
		self.m = None

	def parse_addrs(self, field):
		L = []
		addrs = self.m.get_all(field, [])
		addrs = email.Utils.getaddresses(addrs)
		for name,addr in addrs:
			if name: name = decode_header(name)
			if addr: L.append((name, addr))
		return L

	def parse_headers(self):
		parser = FeedParser()
		while True:
			line = self.f.readline()
			self.hbuf.write(line)
			parser.feed(line)
			if not line.strip(): break
		self.m = parser.close()

		L = self.parse_addrs('from')
		self.h_from = L and L[0] or None
		self.h_to = self.parse_addrs('to')
		self.h_cc = self.parse_addrs('cc')
		self.h_rcpts = self.h_to + self.h_cc
		self.h_subject = decode_header(self.m.get('subject'))
		self.h_subject_len1 = len(self.h_subject)
		self.h_subject = self.h_subject.replace(' ', '')
		self.h_subject = self.h_subject.replace(u'　', '')
		self.h_subject_len2 = len(self.h_subject)

	def show_headers(self):
		print 'From:', str_addrs([self.h_from])
		print 'To:', str_addrs(self.h_to)
		print 'Cc:', str_addrs(self.h_cc)
		print 'Subject:', self.h_subject

	def write_message(self, target):
		target.write(self.hbuf.getvalue())
		while True:
			buf = self.f.read(4096)
			if not buf: break
			target.write(buf)

	def check(self):
		# 也许应该检查收件人不在To/Cc中的情况，但可能是Bcc...
		# 可能要求from地址是shterm.com之类才能bcc更好...
		# 至少先把是否启用检查的规则打开
		x = False
		for name,addr in self.h_rcpts:
			if addr.startswith('list-'): x = True
			if addr in ('garyshi@shterm.com', 'shigl@shterm.com'): x = True
		for addr in self.rcpts:
			if addr.startswith('list-'): x = True
			if addr in ('garyshi@shterm.com', 'shigl@shterm.com'): x = True
		if not x: return True, 'skip check irrelevant address'

		if not self.h_from: return False, 'absent from address'
		if not self.h_to: return False, 'absent to addresses'
		if self.h_from[1] != self.sender:
			logging.warn('sender address mismatch: %s vs. %s' % (self.h_from[1], self.sender))
			#return False, 'sender address mismatch'

		for s in '1234',u'请转',u'转相关',u'转有关',u'转需求',u'老板',u'总经理',u'高级',u'训练',u'培训',u'如何做好',u'详细',u'资料',u'制度',u'模版',u'工具',u'考核',u'必备',u'条例',u'法规',u'团队',u'特训',u'执行力',u'管理',u'招聘',u'面试',u'技巧',u'专业',u'合同':
			if s in self.h_from[0]: return False, 'sender match "%s"' % s

		for s in u'准时开课',u'研修班',u'社保法',u'新任经理',u'用数字说话',u'注塑部',u'实战',u'训练营',u'零缺陷',u'疯狂训练',u'工伤保险',u'车间主任',u'为企业':
			if s in self.h_from[0]: return False, 'sender match "%s"' % s
			if s in self.h_subject: return False, 'subject match "%s"' % s

		for s in u'╭╯',u'╰╮',u'＜',u'＞',u'√',u'╰',u'☆',u'◇',u'┻',u'≡',u'¤':
			if self.h_from[0].startswith(s) or self.h_from[0].endswith(s): return False, 'sender match "%s"' % s
			if self.h_subject.startswith(s) or self.h_subject.endswith(s): return False, 'subject match "%s"' % s

		for s in u'部门经理',u'部门主管':
			if s in self.h_subject: return False, 'subject match "%s"' % s
		for s in u'经理',u'主管':
			if self.h_subject.endswith(s): return False, 'subject match "%s"' % s

		# TODO: really check DomainKey and DKIM
		if self.h_from[1].endswith('@gmail.com'):
			x = self.m.get('DomainKey-Signature')
			if not x or len(x) < 200: return False, 'invalid DomainKey-Signature'
			x = self.m.get('DKIM-Signature')
			if not x or len(x) < 200: return False, 'invalid DKIM-Signature'

		if self.h_from[1].endswith('@163.com'):
			x = self.m.get('x-mailer')
			if not x or not x.lower().startswith('coremail'): return False, '163.com check'
			x = self.m.get('X-Coremail-Antispam')
			if not x or len(binascii.a2b_base64(x)) < 16: return False, '163.com check'

		return True, None

def main(sender, rcpts, f, t):
	cm = CheckingMessage(sender, rcpts, f)
	cm.parse_headers()
	result,reason = cm.check()
	logging.info('CHECK RESULT: %s (%s)' % (result, reason))
	#if result: cm.show_headers()
	if result:
		p = None
		if not t:
			cmdline = '/usr/sbin/sendmail -G -i -f %s -- %s' % (sender, ' '.join(rcpts))
			logging.info('PIPE BACK TO SENDMAIL: %s' % cmdline)
			p = subprocess.Popen(cmdline, shell=True, bufsize=4096, close_fds=True,
				stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
			t = p.stdin
		cm.write_message(t)
		t.close()
		if p: p.wait()
	return result, reason

if __name__ == '__main__':
	if sys.argv[1] == '-t':
		for filename in sys.argv[2:]:
			f = open(filename)
			try:
				t = StringIO()
				result,reason = main(f, t)
				print filename, result, reason
				s = t.getvalue()
				if s: open(filename+'x', 'w').write(s)
			finally: f.close()
			print
	elif sys.argv[1] == '-f':
		formatter = logging.Formatter('%(asctime)s pid=%(process)d %(levelname)s %(message)s')
		handler = logging.handlers.RotatingFileHandler('/tmp/antispam.log', maxBytes=1048576, backupCount=5)
		handler.setFormatter(formatter)
		logger = logging.getLogger(None)
		logger.addHandler(handler)
		logger.setLevel(logging.DEBUG)
		logger.info('PROGRAM START: %s' % sys.argv[1:])
		main(sys.argv[2], sys.argv[3:], sys.stdin, None)
