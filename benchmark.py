#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
\descr: The benchmark, winch optionally generates or preprocesses datasets using specified executable,
	optionally executes specified apps with the specified params on the specified datasets,
	and optionally evaluates results of the execution using specified executable(s).
	
	All executions are traced and logged also as resources consumption: CPU (user, kernel, etc.) and memory (RSS RAM).
	Traces are saved even in case of internal / external interruptions and crashes.
	
	
	Using this generic benchmarking framework,
	= Overlapping Hierarchical Clustering Benchmark =
	is implemented:
	- synthetic datasets are generated using extended LFR Framework (origin: https://sites.google.com/site/santofortunato/inthepress2, 
		which is "Benchmarks for testing community detection algorithms on directed and weighted graphs with overlapping communities"
		by Andrea Lancichinetti 1 and Santo Fortunato)
	- executes HiReCS (www.lumais.com/hirecs), Louvain (original https://sites.google.com/site/findcommunities/ and igraph implementations),
		Oslom2 (http://www.oslom.org/software.htm) and Ganxis/SLPA (https://sites.google.com/site/communitydetectionslpa/) clustering algorithms
		on the generated synthetic networks
	- evaluates results using NMI for overlapping communities, extended versions of:
		* gecmi (https://bitbucket.org/dsign/gecmi/wiki/Home, "Comparing network covers using mutual information" by Alcides Viamontes Esquivel, Martin Rosvall)
		* onmi (https://github.com/aaronmcdaid/Overlapping-NMI, "Normalized Mutual Information to evaluate overlapping community finding algorithms"
		  by  Aaron F. McDaid, Derek Greene, Neil Hurley)
	- resources consumption is evaluated using exectime profiler (https://bitbucket.org/lumais/exectime/)

\author: (c) Artem Lutov <artem@exascale.info>
\organizations: eXascale Infolab <http://exascale.info/>, Lumais <http://www.lumais.com/>, ScienceWise <http://sciencewise.info/>
\date: 2015-04
"""

from __future__ import print_function  # Required for stderr output, must be the first import
import sys
import time
import subprocess
from multiprocessing import cpu_count
import os
import shutil
import signal  # Intercept kill signals
from math import sqrt
import glob
from itertools import chain
import benchapps  # Benchmarking apps (clustering algs)
from benchcore import *

# Add 3dparty modules
sys.path.insert(0, '3dparty')
from tohig import tohig
#from functools import wraps

from benchcore import _syntdir
from benchcore import _extexectime
from benchcore import _extclnodes
from benchcore import _execpool
from benchcore import _netshuffles


_extnetfile = '.nsa'  # Extension of the network files to be executed by the algorithms; Network specified by tab/space separated arcs
#_algseeds = 9  # TODO: Implement


def parseParams(args):
	"""Parse user-specified parameters
	
	return
		gensynt  - generate synthetic networks:
			0 - do not generate
			1 - generate only if this network is not exist
			2 - force geration (overwrite all)
		convnets  - convert existing networks into the .hig format
			0 - do not convert
			1 - convert only if this network is not exist
			2 - force conversion (overwrite all)
		udatas  - list of unweighted datasets to be run
		wdatas  - list of weighted datasets to be run
		timeout  - execution timeout in sec per each algorithm
		algorithms  - algorithms to be executed (just names as in the code)
	"""
	assert isinstance(args, (tuple, list)) and args, 'Input arguments must be specified'
	gensynt = 0
	convnets = 0
	runalgs = False
	evalres = False
	udatas = []
	wdatas = []
	timeout = 36 * 60*60  # 36 hours
	timemul = 1  # Time multiplier, sec by default
	algorithms = None
	
	for arg in args:
		# Validate input format
		if arg[0] != '-':
			raise ValueError('Unexpected argument: ' + arg)
		
		if arg[1] == 'g':
			if arg not in '-gf':
				raise ValueError('Unexpected argument: ' + arg)
			gensynt = len(arg) - 1  # '-gf'  - forced generation (overwrite)
		elif arg[1] == 'a':
			if not (arg[0:3] == '-a=' and len(arg) >= 4):
				raise ValueError('Unexpected argument: ' + arg)
			algorithms = arg[3:].split()
		elif arg[1] == 'c':
			if arg not in '-cf':
				raise ValueError('Unexpected argument: ' + arg)
			convnets = len(arg) - 1  # '-cf'  - forced conversion (overwrite)
		elif arg[1] == 'r':
			if arg != '-r':
				raise ValueError('Unexpected argument: ' + arg)
			runalgs = True
		elif arg[1] == 'e':
			if arg != '-e':
				raise ValueError('Unexpected argument: ' + arg)
			evalres = True
		elif arg[1] == 'd' or arg[1] == 'f':
			pos = arg.find('=', 2)
			if pos == -1 or arg[2] not in 'uw=' or len(arg) == pos + 1:
				raise ValueError('Unexpected argument: ' + arg)
			pos += 1
			# Extend weighted / unweighted dataset, default is unweighted
			(wdatas if arg[2] == 'w' else udatas).append(arg[pos+1:])
		elif arg[1] == 't':
			pos = arg.find('=', 2)
			if pos == -1 or arg[2] not in 'smh=' or len(arg) == pos + 1:
				raise ValueError('Unexpected argument: ' + arg)
			pos += 1
			if arg[2] == 'm':
				timemul = 60  # Minutes
			elif arg[2] == 'h':
				timemul = 3600  # Hours
			timeout = int(arg[pos:]) * timemul
		else:
			raise ValueError('Unexpected argument: ' + arg)
			
	return gensynt, convnets, runalgs, evalres, udatas, wdatas, timeout, algorithms


def generateNets(overwrite=False):
	"""Generate synthetic networks with ground-truth communities and save generation params
	
	overwrite  - whether to overwrite existing networks or use them
	"""
	paramsdir = 'params/'
	
	assert _syntdir[-1] == '/' and paramsdir[-1] == '/', "Directory name must have valid terminator"
	paramsDirFull = _syntdir + paramsdir
	if not os.path.exists(paramsDirFull):
		os.makedirs(paramsDirFull)
	# Initial options for the networks generation
	N0 = 1000;  # Satrting number of nodes
	
	evalmaxk = lambda genopts: int(round(sqrt(genopts['N'])))
	evalmuw = lambda genopts: genopts['mut'] * 2/3
	evalminc = lambda genopts: 5 + int(genopts['N'] / N0)
	evalmaxc = lambda genopts: int(genopts['N'] / 3)
	evalon = lambda genopts: int(genopts['N'] * genopts['mut']**2)
	# Template of the generating options files
	genopts = {'mut': 0.275, 'beta': 1.35, 't1': 1.65, 't2': 1.3, 'om': 2, 'cnl': 1}
	
	# Generate options for the networks generation using chosen variations of params
	varNmul = (1, 2, 5, 10, 25, 50)  # *N0
	vark = (5, 10, 20)
	global _execpool
	
	_execpool = ExecPool(max(cpu_count() - 1, 1))
	netgenTimeout = 10 * 60  # 10 min
	for nm in varNmul:
		N = nm * N0
		for k in vark:
			name = 'K'.join((str(nm), str(k)))
			ext = '.ngp'  # Network generation parameters
			fnamex = name.join((paramsDirFull, ext))
			if not overwrite and os.path.exists(fnamex):
				assert os.path.isfile(fnamex), '{} should be a file'.format(fnamex)
				continue
			print('Generating {} parameters file...'.format(fnamex))
			with open(fnamex, 'w') as fout:
				genopts.update({'N': N, 'k': k})
				genopts.update({'maxk': evalmaxk(genopts), 'muw': evalmuw(genopts), 'minc': evalminc(genopts)
					, 'maxc': evalmaxc(genopts), 'on': evalon(genopts), 'name': name})
				for opt in genopts.items():
					fout.write(''.join(('-', opt[0], ' ', str(opt[1]), '\n')))
			if os.path.isfile(fnamex):
				args = ('../exectime', '-n=' + name, ''.join(('-o=', paramsdir, 'lfrbench_uwovp', _extexectime))
					, './lfrbench_uwovp', '-f', name.join((paramsdir, ext)))
				#Job(name, workdir, args, timeout=0, ontimeout=0, onstart=None, ondone=None, tstart=None)
				if _execpool:
					_execpool.execute(Job(name=name, workdir=_syntdir, args=args, timeout=netgenTimeout, ontimeout=1
						, onstart=lambda job: shutil.copy2(_syntdir + 'time_seed.dat', name.join((_syntdir, '.ngs')))))  # Network generation seed
	print('Parameter files generation is completed')
	if _execpool:
		_execpool.join(2 * 60*60)  # 2 hours
		_execpool = None
	print('Synthetic networks files generation is completed')
	

def convertNets(datadir, overwrite=False):
	"""Gonvert input networks to another formats
	
		datadir  - directory of the networks to be converted
		overwrite  - whether to overwrite existing networks or use them
	"""
	print('Converting networks from {} into the required formats (.hig, .lig, etc.)...'
		.format(datadir))
	# Convert network files into .hig format and .lig (Louvain Input Format)
	for net in glob.iglob('*'.join((datadir, _extnetfile))):
		try:
			# Convert to .hig format
			tohig(net, ('-f=tsa'))  # Network in the tab separated weighted arcs format
		except StandardError as err:
			print('ERROR on "{}" conversion into .hig, the network is skipped: {}'.format(net, err), file=sys.stderr)
		
		netnoext = os.path.splitext(net)[0]  # Remove the extension

		## Confert to Louvain binaty input format
		#try:
		#	# ./convert [-r] -i graph.txt -o graph.bin -w graph.weights
		#	# r  - renumber nodes
		#	# ATTENTION: original Louvain implementation processes incorrectly weighted networks with uniform weights (=1) if supplied as unweighted
		#	subprocess.call([_algsdir + 'convert', '-i', net, '-o', netnoext + '.lig'
		#		, '-w', netnoext + '.liw'])
		#except StandardError as err:
		#	print('ERROR on "{}" conversion into .lig, the network is skipped: {}'.format(net), err, file=sys.stderr)
		
		# Make shuffled copies of the input networks for the Louvain_igraph
		#if not os.path.exists(netnoext) or overwrite:
		print('Shuffling {} into {} {} times...'.format(net, netnoext, _netshuffles))
		if not os.path.exists(netnoext):
			os.makedirs(netnoext)
		netname = os.path.split(netnoext)[1]
		assert netname, 'netname should be defined'
		for i in range(_netshuffles):
			# sort -R pgp_udir.net -o pgp_udir_rand3.net
			subprocess.call(['sort', '-R', net, '-o'
				, ''.join((netnoext, '/', netname, '_', str(i), _extnetfile))])
		#else:
		#	print('The shuffling is skipped: {} is already exist'.format(netnoext))
				
	print('Networks conversion is completed')


def unknownApp(name):
	"""A stub for the unknown / not implemented apps (algorithms) to be benchmaked
	
	name  - name of the funciton to be called (traced and skipped)
	"""
	def stub(*args):
		print(' '.join(('ERROR: ', name, 'function is not implemented, the call is skipped.')), file=sys.stderr)
	stub.__name__ = name  # Set original name to the stub func
	return stub


def terminationHandler(signal, frame):
	"""Signal termination handler"""
	global _execpool
	
	#if signal == signal.SIGABRT:
	#	os.killpg(os.getpgrp(), signal)
	#	os.kill(os.getpid(), signal)
	
	if _execpool:
		del _execpool
		_execpool = None
	sys.exit(0)


def benchmark(*args):
	""" Execute the benchmark
	
	Run the algorithms on the specified datasets respecting the parameters.
	"""
	exectime = time.time()
	gensynt, convnets, runalgs, evalres, udatas, wdatas, timeout, algorithms = parseParams(args)
	print('The benchmark is started, parsed params:\n\tgensynt: {}\n\tconvnets: {}'
		'\n\trunalgs: {}\n\tevalres: {}\n\tudatas: {}\n\twdatas: {}\n\ttimeout: {}\n\talgorithms: {}'
		.format(gensynt, convnets, runalgs, evalres, ', '.join(udatas), ', '.join(wdatas), timeout, algorithms))
	# TODO: Implement consideration of udata, wdata (or just datadir - for some algs weighted/unweighted are defined from the file)
	
	if gensynt:
		generateNets(gensynt == 2)  # 0 - do not generate, 1 - only if not exists, 2 - forced generation
		
	if convnets:
		convertNets(_syntdir, convnets == 2)  # 0 - do not convert, 1 - only if not exists, 2 - forced conversion
		
	global _execpool
	appsmodule = benchapps  # Module with algorithms definitions to be run; sys.modules[__name__]
	
	# Run the algorithms and measure their resource consumption
	if runalgs:
		# Run algs on synthetic datasets
		#udatas = ['../snap/com-dblp.ungraph.txt', '../snap/com-amazon.ungraph.txt', '../snap/com-youtube.ungraph.txt']
		assert not _execpool, '_execpool should be clear on algs execution'
		_execpool = ExecPool(max(min(4, cpu_count() - 1), 1))
		netsnum = 1
	
		if not algorithms:
			#algs = (execLouvain, execHirecs, execOslom2, execGanxis, execHirecsNounwrap)
			#algs = (execHirecsNounwrap,)  # (execLouvain, execHirecs, execOslom2, execGanxis, execHirecsNounwrap)
			# , execHirecsOtl, execHirecsAhOtl, execHirecsNounwrap)  # (execLouvain, execHirecs, execOslom2, execGanxis, execHirecsNounwrap)
			algs = [getattr(appsmodule, func) for func in dir(appsmodule) if func.startswith('exec')]
		else:
			algs = [getattr(appsmodule, 'exec' + alg.capitalize(), unknownApp('exec' + alg.capitalize())) for alg in algorithms]
		algs = tuple(algs)

		for net in glob.iglob('*'.join((_syntdir, _extnetfile))):
			for alg in algs:
				try:
					alg(_execpool, net, timeout)
				except StandardError as err:
					errexectime = time.time() - exectime
					print('The {} is interrupted by the exception: {} on {:.4f} sec ({} h {} m {:.4f} s)'
						.format(alg.__name__, err, errexectime, *secondsToHms(errexectime)))
				else:
					netsnum += 1
		
		## Additionally execute Louvain multiple times
		#alg = execLouvain
		#if alg in algs:
		#	for net in glob.iglob('*'.join((_syntdir, _extnetfile))):
		#		for execnum in range(1, 10):
		#			try:
		#				alg(_execpool, net, timeout, execnum)
		#			except StandardError as err:
		#				errexectime = time.time() - exectime
		#				print('The {} is interrupted by the exception: {} on {:.4f} sec ({} h {} m {:.4f} s)'
		#					.format(alg.__name__, err, errexectime, *secondsToHms(errexectime)))
		
		# TODO: Implement execution on custom datasets considering whether they weighted / unweighted			
		## Run algs on the specified datasets if required
		## Unweighted networks
		#for udat in udatas:
		#	if not os.path.exists(udat):
		#		print('WARNING, "{}" does not exist, skipped', file=sys.stderr)
		#	#if os.path.isdir(udat):
		#	#	fnames = glob.iglob('*'.join((_syntdir, _extnetfile))):


		if _execpool:
			_execpool.join(timeout * netsnum)
			_execpool = None
		exectime = time.time() - exectime
		print('The benchmark execution is successfully comleted on {:.4f} sec ({} h {} m {:.4f} s)'
			.format(exectime, *secondsToHms(exectime)))
	
	# Evaluate results (NMI)
	if evalres:
		print('Starting NMI evaluation...')
		if not algorithms:
			#evalalgs = (evalLouvain, evalHirecs, evalOslom2, evalGanxis
			#				, evalHirecsNS, evalOslom2NS, evalGanxisNS)
			#evalalgs = (evalHirecs, evalHirecsOtl, evalHirecsAhOtl
			#				, evalHirecsNS, evalHirecsOtlNS, evalHirecsAhOtlNS)
			evalalgs = [getattr(appsmodule, func) for func in dir(appsmodule) if func.startswith('eval')]
		else:
			evalalgs = chain(*[(getattr(appsmodule, 'eval' + alg.capitalize(), unknownApp('eval' + alg.capitalize())),
				getattr(appsmodule, ''.join(('eval', alg.capitalize(), 'NS')), unknownApp(''.join(('eval', alg.capitalize(), 'NS')))))
				for alg in algorithms])
		evalalgs = tuple(evalalgs)

		assert not _execpool, '_execpool should be clear on algs evaluation'
		_execpool = ExecPool(max(cpu_count() - 1, 1))
		netsnum = 0
		timeout = 20 *60*60  # 20 hours
		for cndfile in glob.iglob('*'.join((_syntdir, _extclnodes))):
			for elg in evalalgs:
				try:
					print('Starting ' + str(elg))
					elg(_execpool, cndfile, timeout)
				except StandardError as err:
					print('The {} is interrupted by the exception: {}'
						.format(_execpool.__name__, err))
				else:
					netsnum += 1
		if _execpool:
			_execpool.join(max(max(timeout, exectime * 2), timeout + 60 * netsnum))  # Twice the time of algorithms execution
			_execpool = None
		print('NMI evaluation is completed')
	print('The benchmark is completed')


if __name__ == '__main__':
	if len(sys.argv) > 1:
		# Set handlers of external signals
		signal.signal(signal.SIGTERM, terminationHandler)
		signal.signal(signal.SIGHUP, terminationHandler)
		signal.signal(signal.SIGINT, terminationHandler)
		signal.signal(signal.SIGQUIT, terminationHandler)
		signal.signal(signal.SIGABRT, terminationHandler)
		benchmark(*sys.argv[1:])
	else:
		print('\n'.join(('Usage: {0} [-g[f] [-c[f]] [-r] [-e] [-d{{u,w}}=<datasets_dir>] [-f{{u,w}}=<dataset>] [-t[{{s,m,h}}]=<timeout>]',
			'  -g[f]  - generate synthetic datasets in the {syntdir}',
			'    Xf  - force the generation even when the data is already exist',
			'  -a[="app1 app2 ..."]  - apps to benchmark among the implemented.'
			' Impacts -{{c, r, e}} options. Optional, all apps are executed by default',
			'  -c[f]  - convert existing networks into the .hig, .lig, etc. formats',
			'    Xf  - force the conversion even when the data is already exist',
			'  -r  - run the benchmarking apps on the prepared data',
			'  -e  - evaluate the results through the measurements',
			'  -d[X]=<datasets_dir>  - directory of the datasets',
			'  -f[X]=<dataset>  - dataset file name',
			'    Xu  - the dataset is unweighted. Default option',
			'    Xw  - the dataset is weighted',
			'    Notes:',
			'    - multiple directories and files can be specified via multiple -d/f options (one per the item)',
			'    - datasets should have the following format: <node_src> <node_dest> [<weight>]',
			'  -t[X]=<number>  - specifies timeout per each benchmarking application in sec, min or hours. Default: 0 sec',
			'    Xs  - time in seconds. Default option',
			'    Xm  - time in minutes',
			'    Xh  - time in hours',
			)).format(sys.argv[0], syntdir=_syntdir))
