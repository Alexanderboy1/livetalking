# 数字人课堂部署

课堂服务增加 PDF/PPTX 课件准备、逐页讲稿、共享数字人直播、主持人审核、观众文字提问和课程上下文问答。一个课堂复用一份数字人推理会话，观众通过 WebRTC 独立接收音视频。

## 服务器依赖

沿用项目当前支持的 Python、CUDA 驱动和 CUDA 版 PyTorch 环境。先按服务器驱动版本安装项目支持的 PyTorch GPU wheel，再安装仓库依赖：

```bash
pip install torch==2.9.1 torchvision==0.24.1 torchaudio==2.9.1 --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
```

上面的 PyTorch 命令对应项目 README 当前列出的 CUDA 12.8 示例，其他驱动请从 PyTorch 官方历史版本页面选兼容版本。数字人模型权重和 `data/avatars` 仍需按原项目说明准备。

PPTX 转换依赖 LibreOffice Impress；PDF 导入不依赖 LibreOffice。Ubuntu/Debian 可安装：

```bash
sudo apt-get update
sudo apt-get install -y libreoffice-impress fonts-noto-cjk
```

新增 Python 依赖为 `PyMuPDF` 和 `python-pptx`。`aiohttp`、`aiortc`、PyAV 已是现有项目依赖。

## 启动

设置 LLM 密钥、生成主持人密钥并用 WebRTC 模式启动：

```bash
export DASHSCOPE_API_KEY='你的 DashScope API Key'
export LIVETALKING_CLASSROOM_ADMIN_TOKEN="$(openssl rand -hex 32)"
export LIVETALKING_COURSE_DIR="$PWD/data/courses"
python app.py --transport webrtc --model wav2lip --avatar_id wav2lip256_avatar1 --max_session 5 --listenport 8010
```

如使用其他 OpenAI 兼容服务，按现有配置设置 `--llm_provider` 和 `--llm_model`。每个运行中的课堂占用一个 `--max_session` 数量，GPU 显存能支持的课堂数取决于当前数字人模型和分辨率。

在 `/classroom` 使用主持人控制台，观众使用主持人分享的课堂链接。生产部署应通过 HTTPS 反向代理。WebRTC 需要服务器对观众网络可达的 ICE/UDP 媒体连接；当前版本仅接入已有 --stun 配置，没有 TURN 用户名/密码配置入口。限制严格的网络需另外扩展 TURN 配置，或先使用具备公网 UDP 连通性的服务器。配置云安全组、防火墙和 STUN 后，需从校外网络实测媒体连接。SSE 路由为 `/api/classrooms/{id}/events`；Nginx 需为 `/api/classrooms/` 关闭响应缓冲并允许长连接。

当前讲次、问答队列和进度保存在单进程内存；课程与讲稿持久化到磁盘。部署一个应用进程即可；暂不支持多 worker/副本、重启后恢复课堂或多个进程共享 GPU。课程与渲染页保存在 `data/courses/`，请放在持久磁盘并备份。设置 `LIVETALKING_TRUST_PROXY=1` 前，必须确认可信反向代理会覆盖而非追加 `X-Real-IP`；未配置时按服务器看到的源 IP 进行限速。

## 授课流程

1. 导入最多 50 MB、100 页的 PDF/PPTX。优先使用 PPT 备注作为逐页讲稿，没有备注时从课件页提取文字。
2. 检查并编辑每页讲稿，图像型课件需要人工填写讲稿，然后保存。
3. 创建课堂并分享观众链接，点击播放音视频，然后开始授课。
4. 主持人审核问题。获批问题会在当前语音段落之后回答，然后从原讲稿继续。暂停和手动跳页也在当前语音段或回答结束后执行。
5. 结束课堂以释放数字人会话；课程和讲稿仍保存在服务器。

控制台提供纯画面链接（OBS 窗口采集），但该页面仍通过浏览器 WebRTC 接收媒体，不是 RTMP 推流端。

## 当前范围

课件以静态 PNG 页面展示，不保留动画、内嵌视频或转场；问题入口为文字，不包括语音提问和第三方直播平台弹幕接入。默认由主持人审核，可在单个课堂开启自动通过。IP 限速是基础保护，不能替代观众登录、WAF 或专门的内容审核服务。

## 部署前检查与验收

在项目目录执行：

```bash
python scripts/check_classroom.py
python -m unittest discover -s tests -p 'test_classroom*.py' -v
```

检查脚本验证基础依赖、GPU、默认 Wav2Lip 模型和主持人/LLM 配置；其他数字人模型需检查相应权重。测试使用模拟数字人，不消耗 GPU，也不调用付费 LLM/TTS。上线前请在 GPU 服务器用两个浏览器实测：上传多页课件、同时播放、音频结束自动翻页、观众提问审核、回答后续讲、暂停/继续和结束释放资源。

每个课堂最多接受 32 个 WebRTC 连接，这是连接保护上限，不是性能保证；编码和网络带宽随观众增长。大规模分发需要媒体 SFU。反向代理示例见 [classroom.nginx.conf](../deploy/classroom.nginx.conf)。它仅供并入已有 HTTPS 配置使用；8010 端口只应对代理和可信网络开放。
