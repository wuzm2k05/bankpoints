# websocket protocol
后端通过websocket提供服务。前端和后端之间的通信都是通过websoket通信。

## 通信的基本方式
一个前端可以和后端建立一个或者多个websocket。通信方式为前端发送request给后端，后端通过返回response给前端。
request和response是局限在一个websocket中的，如果一个websocket中断，那么这个websocket中前面未处理完的request将不再有response返回给前端。
一个websocekt中的request和response是pipeline形式的，即发送了一个请求不必等response就可以发送下一个请求。但后端不保证 Response 的返回顺序与 Request 的发送顺序完全一致（异步处理），前端需根据 request中的seq 字段进行逻辑匹配。
**但是如果一个request返回多个response，那么这些response的顺序一定是按照逻辑顺序返回的。**

## request/response
request和response都应当是json的编码方式。
### request/response通用介绍
request中必须包含下面内容：
```
{
    "seq"："identifier of the msg"
    "type": "type of request"
    
}
```
seq： 连接范围内唯一的请求标识。一个websocket中所有的request的seq不能重复。这个需要前端提供并且保证。
userCode： 用户标识。同一个用户每次必须使用一样的id。

response中包含的通用内容：
```
{
    "seq"："identifier of the msg"
    "type": "type of request"

    "status": "success / fail / end"
    "errorCode": "error code of failure" [optional]
    "errorMsg": "error information to explain the error" [optional]
    
}
```
其中seq和type都是从request中copy过来的，用来让前端对应相应的request。
errorCode和errorMsg是在response失败的情况下才会有。
status: success 和 end都表示成功的状态。有些request需要多个response，那么success表示后面还有response，end表示是最后一个response。对于只有一个response的，status都是end。

**下面介绍的request类别，没有特别说明的，都是只返回一个response**


### load user chat history
Requst： 除通用内容外，需要下面内容
```
{
    "type": "loadUserHistory"
    "userCode": "identifier of user"
}
```
type:必须填loadUserHistory。
userCode： 用户的唯一标识。后端利用这个标识找到这个用户前面的聊天记录。

Response: 除通用内容外，包含下面内容
```
{
    "userCode": "identifier of user"
    "history": [ {"role":"assistant or user", "content": "content of message"},]
}
```

userCode使用的是Request中的值。
history 是一个数组包括多条消息。每条消息包括role和content两项内容。role可以是assistant或者user，content是具体的内容信息，以markdown格式回答。  
assistant role表示是AI说的内容。user role表示是用户说的内容。

说明：用户历史存储时长可配置（后端统一配置），默认为一天。

### chat
Requst： 除通用内容外，需要下面内容
```
{
    "type": "chat"
    "userCode": "identifier of user"
    "prompt": "prompt of user",
}
```
type: 必须填chat。
userCode: 参照loadUserHistory的说明。后端会存储这个用户的说明上下文，使用userCode来使用对应的上下文以便更好理解用户此次的目的。
prompt：用户此次的输入。用户可能进行多轮对话，这里面仅包含此次用户的输入。（原来的输入和回答都会存在后端的用户上下文中）

Response：除通用内容外，包含下面内容
```
{
    "userCode": "identifier of user"

    "answer": "answer of AI",

    "status": "success / fail / end"
}
```
userCode: 同loadUserHistory。
answer: AI对用户请求的本次回答。以markdown格式返回。

chat请求的response可能是多个。最后一个的status会标记为end。每个response的answer包含了部分的内容，后端保证按逻辑顺序（分片顺序）发送。前端需按接收顺序拼接 answer 内容。
如果中间出现错误，返回了status为fail的response，那么后续不会有response了，当然也不会有status为end的response。
