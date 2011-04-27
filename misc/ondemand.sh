#!/usr/bin/sudo /bin/sh
for CPUFREQ in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
do
	[ -f $CPUFREQ ] || continue
	echo -n ondemand > $CPUFREQ
done
