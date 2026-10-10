# start Redis (as root)
note: the file name need to be changed in test env. redis-conf to redis.conf
```
cd /root/software/redis
nohup /usr/local/bin/redis-server ./redis-conf &
```

port:
nginx                             internal
api.ninenode.com:9443              8446 (token port)
api.ninenode.com:443               8445 (msg port)

www.node09.cn:9443                 8445(msg port)


# build eggs RAG embedding db
python tools_cmd.py egg_build


# build voucher knowledge 
# 更新立减金向量数据库
# 1. 更改txt文件（例子在materials/voucher_knowledge.txt)
# 2. 运行下面命令生成embedding的数据库
python tools_cmd.py bvd <voucher_txt_file_name>
