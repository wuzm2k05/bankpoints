# start Redis (as root)
note: the file name need to be changed in test env. redis-conf to redis.conf
```
cd /root/software/redis
nohup /usr/local/bin/redis-server ./redis-conf &
```