#!/bin/bash

# usage:
#  ./datamounter [-w]
#
#  by default, datamounter remounts the data partition as read only.
#  with the -w switch, datamounter remounts read-write
#

MOUNT_WRITABLE=
PARTITION=/dev/mmcblk0p3
MOUNT_POINT=/home/door/doorwoman

while getopts "w" opt; do
  case "$opt" in
    w)
        MOUNT_WRITABLE=YES
        ;;
  esac
done

pidf=/tmp/$(basename ${0}).pid
exec 221>${pidf}
flock --exclusive 221 ||
{
  echo "$(basename ${0}): failed to get lock"
  exit 1
}
echo ${$}>&221

if [[ "$MOUNT_WRITABLE" ]]; then
  # TODO Increment lockfile for parallel write users
  if [ $(grep $MOUNT_POINT /proc/mounts |grep "\sro[\s,]" | wc -l) -gt 0 ]; then
    echo "$(basename ${0}): remounting rw"
    sudo mount -o remount,rw $PARTITION $MOUNT_POINT
  else
      echo "$(basename ${0}): partition is already writable"
  fi
else
  # TODO Deincrement lockfile for parallel write users
  if [ $(grep $MOUNT_POINT /proc/mounts |grep "\srw[\s,]" | wc -l) -gt 0 ]; then
    echo "$(basename ${0}): remounting ro"
    sudo mount -o remount,ro $PARTITION $MOUNT_POINT
  else
    echo "$(basename ${0}): partition is already read-only"
  fi
fi
