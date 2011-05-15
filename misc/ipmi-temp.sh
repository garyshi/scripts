#!/bin/sh
IPADDR=$1
# 15: SYS Temp(ESB2) (Temperature): 33.00 C (NA/70.00): [OK]
# 16: FBD 1.8 Temp (Temperature): 33.00 C (NA/70.00): [OK]
# 17: Branch1 Temp (Temperature): 28.00 C (NA/70.00): [OK]
# 18: Ambient Temp (Temperature): 21.00 C (NA/70.00): [OK]
i=0
for t in `/usr/sbin/ipmi-sensors -h $IPADDR -u super -p config -s "15 16 17 18" | sed -e 's/.*: \([0-9.]*\) C.*/\1/'`; do
	i=$[i+1]
	case "$i" in
		1) n=temp_ioctl ;;
		2) n=temp_mem ;;
		3) n=temp_branch1 ;;
		4) n=temp_ambient ;;
	esac
	echo $n $t
	#gmetric -t float -u Celcius -n $n -v $t
done
