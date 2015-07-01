# -*- Mode:Python; indent-tabs-mode:t; tab-width:4 -*-

import glob
import os
import snapcraft.common
import snapcraft.plugin
import subprocess
import sys
import tempfile
import time
import yaml

def init(args):
	if os.path.exists("snapcraft.yaml"):
		snapcraft.common.log("snapcraft.yaml already exists!", file=sys.stderr)
		sys.exit(1)
	yaml = 'parts:\n'
	for partName in args.part:
		part = snapcraft.plugin.loadPlugin(partName, partName, loadCode=False)
		yaml += '    ' + part.names()[0] + ':\n'
		for opt in part.config.get('options', []):
			if part.config['options'][opt].get('required', False):
				yaml += '        ' + opt + ':\n'
	yaml = yaml.strip()
	with open('snapcraft.yaml', mode='w+') as f:
		f.write(yaml)
	snapcraft.common.log("Wrote the following as snapcraft.yaml.")
	print()
	print(yaml)
	sys.exit(0)

def allOthers(args):
	systemPackages = []
	includedPackages = []
	allParts = []
	afterRequests = {}

	def loadPlugin(partName, pluginName, properties, loadCode=True):
		global allParts, systemPackages, includedPackages

		part = snapcraft.plugin.loadPlugin(pluginName, partName, properties, loadCode=loadCode)

		systemPackages += part.config.get('systemPackages', [])
		includedPackages += part.config.get('includedPackages', [])
		allParts.append(part)
		return part

	cmds = [args.cmd]
	forceAll = args.force
	forceCommand = None

	if cmds[0] == "all":
		cmds = ['snap']

	if cmds[0] in snapcraft.common.commandOrder:
		forceCommand = cmds[0]
		cmds = snapcraft.common.commandOrder[0:snapcraft.common.commandOrder.index(cmds[0])+1]

	data = yaml.load(open("snapcraft.yaml", 'r'))
	systemPackages = data.get('systemPackages', [])
	includedPackages = data.get('includedPackages', [])

	for partName in data.get("parts", []):
		properties = data["parts"][partName]

		pluginName = properties.get("plugin", partName)
		if "plugin" in properties: del properties["plugin"]

		if "after" in properties:
			afterRequests[partName] = properties["after"]
			del properties["after"]

		loadPlugin(partName, pluginName, properties)

	# Grab all required dependencies, if not already specified
	newParts = allParts.copy()
	while newParts:
		part = newParts.pop(0)
		requires = part.config.get('requires', [])
		for requiredPart in requires:
			alreadyPresent = False
			for p in allParts:
				if requiredPart in p.names():
					alreadyPresent = True
					break
			if not alreadyPresent:
				newParts.append(loadPlugin(requiredPart, requiredPart, {}))

	# Now sort them
	partsToSort = allParts.copy()
	while partsToSort:
		part = partsToSort.pop(0)
		requires = part.config.get('requires', [])
		for requiredPart in requires:
			for i in range(len(allParts)):
				if requiredPart in allParts[i].names():
					allParts.insert(0, allParts.pop(i))
					break
		afterNames = afterRequests.get(part.names()[0], [])
		for afterName in afterNames:
			for i in range(len(allParts)):
				if afterName in allParts[i].names():
					allParts.insert(0, allParts.pop(i))
					break

	# Install local packages that we need
	if systemPackages:
		newPackages = []
		for checkpkg in systemPackages:
			if subprocess.call(['dpkg-query', '-s', checkpkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
				newPackages.append(checkpkg)
		if newPackages:
			print("Installing required packages on the host system: " + ", ".join(newPackages))
			subprocess.call(['sudo', 'apt-get', 'install'] + newPackages, stdout=subprocess.DEVNULL)

	if includedPackages:
		class Options: pass
		options = Options()
		setattr(options, 'includedPackages', includedPackages)
		pluginDir = os.path.abspath(os.path.join(__file__, "..", "..", "plugins"))
		part = snapcraft.plugin.Plugin(pluginDir, 'snapcraft', 'includedPackages', {}, optionsOverride=options, loadConfig=False)
		part.includedPackages = includedPackages
		allParts = [part] + allParts

	env = []
	env.append("PATH=\"%s/bin:%s/usr/bin:$PATH\"" % (snapcraft.common.stagedir, snapcraft.common.stagedir))
	env.append("LD_LIBRARY_PATH=\"%s/lib:%s/usr/lib:$LD_LIBRARY_PATH\"" % (snapcraft.common.stagedir, snapcraft.common.stagedir))
	env.append("CFLAGS=\"-I%s/include $CFLAGS\"" % snapcraft.common.stagedir)
	env.append("LDFLAGS=\"-L%s/lib $LDFLAGS\"" % snapcraft.common.stagedir)

	if cmds[0] == "shell":
		for part in allParts:
			env += part.env()
		snapcraft.common.env = env
		userCommand = ' '.join(cmds[1:])
		if not userCommand:
			userCommand = "/usr/bin/env PS1='\[\e[1;32m\]snapcraft:\w\$\[\e[0m\] ' /bin/bash --norc"
		snapcraft.common.run(userCommand)

	elif cmds[0] == "assemble":
		snapcraft.common.run(
				"cp -arv %s %s" % (data["snap"]["meta"], snapcraft.common.snapdir))

		# wrap all included commands
		for part in allParts:
			env += part.env()
		snapcraft.common.env = env
		script = "#!/bin/sh\n%s\nexec %%s $*" % snapcraft.common.assembleEnv().replace(snapcraft.common.stagedir, "$SNAP_APP_PATH")

		def wrapBins(bindir):
			absbindir = os.path.join(snapcraft.common.snapdir, bindir)
			if not os.path.exists(absbindir):
				return
			for exe in os.listdir(absbindir):
				if exe.endswith('.real'):
					continue
				exePath = os.path.join(absbindir, exe)
				try: os.remove(exePath + '.real')
				except: pass
				os.rename(exePath, exePath + '.real')
				with open(exePath, 'w+') as f:
					f.write(script % ('"$SNAP_APP_PATH/' + bindir + '/' + exe + '.real"'))
				os.chmod(exePath, 0o755)
		wrapBins('bin')
		wrapBins('usr/bin')

		snapcraft.common.run("snappy build " + snapcraft.common.snapdir)


	elif cmds[0] == "run":
			qemudir = os.path.join(os.getcwd(), "image")
			qemu_img = os.path.join(qemudir, "15.04.img")
			if not os.path.exists(qemu_img):
					try: os.makedirs(qemudir)
					except FileExistsError: pass
					snapcraft.common.run(
							"sudo ubuntu-device-flash core --developer-mode --enable-ssh 15.04 -o %s" % qemu_img,
							cwd=qemudir)
			qemu = subprocess.Popen(
					["kvm", "-m", "768", "-nographic",
					 "-snapshot", "-redir", "tcp:8022::22", qemu_img],
					stdin=subprocess.PIPE)
			n = tempfile.NamedTemporaryFile()
			ssh_opts = [
					"-oStrictHostKeyChecking=no",
					"-oUserKnownHostsFile=%s" % n.name
			]
			while True:
					ret_code =subprocess.call(
							["ssh"]+ssh_opts+
							["ubuntu@localhost", "-p", "8022", "true"])
					if ret_code == 0:
							break
					print("Waiting for device")
					time.sleep(1)
			snap_dir = os.path.join(os.getcwd(), "snap")
			# copy the snap
			snaps = glob.glob(snap_dir+"/*.snap")
			subprocess.call(
					["scp"]+ssh_opts+[
							"-P", "8022", "-r"]+snaps+["ubuntu@localhost:~/"])
			# install the snap
			ret_code =subprocess.call(
					["ssh"]+ssh_opts+
					["ubuntu@localhost", "-p", "8022", "sudo snappy install  *.snap"])
			# "login"
			subprocess.call(
					["ssh"]+ssh_opts+["-p", "8022", "ubuntu@localhost"],
					preexec_fn=os.setsid)
			qemu.kill()
	else:
		for part in allParts:
			env += part.env()
			snapcraft.common.env = env
			for cmd in cmds:
				force = forceAll or cmd == forceCommand
				if not getattr(part, cmd)(force=force):
					snapcraft.common.log("Failed doing %s for %s!" % (cmd, part.names()[0]))
					sys.exit(1)
