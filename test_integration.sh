#!/bin/bash
# 集成测试：验证 freepornvideos 解析功能（无需实际下载）
set -e

echo "=== 集成测试：freepornvideos 支持 ==="
echo

echo "1. 测试单元测试套件..."
python3 test_fpv.py -q
echo "✓ 所有单元测试通过"
echo

echo "2. 验证 Python 模块语法..."
python3 -m py_compile download_fpv.py
python3 -m py_compile ph_server.py
echo "✓ 语法检查通过"
echo

echo "3. 测试 URL 检测..."
python3 -c "
from download_fpv import is_fpv_page
assert is_fpv_page('https://www.freepornvideos.xxx/videos/93810393/title/')
assert not is_fpv_page('https://www.pornhub.com/view_video.php?viewkey=abc')
print('✓ URL 检测正常')
"
echo

echo "4. 测试 MP4 探测逻辑..."
python3 -c "
from download_fpv import probe_mp4_dimensions
import struct

# 构造简化 tkhd box
def make_tkhd(w, h):
    payload = bytearray()
    payload.append(0)  # version
    payload.extend(b'\x00\x00\x00')  # flags
    payload.extend(b'\x00' * 20)  # 时间/id/duration
    payload.extend(b'\x00' * 16)  # reserved/layer/volume
    payload.extend(b'\x00\x01\x00\x00' * 9)  # matrix
    payload.extend(struct.pack('>I', w << 16))
    payload.extend(struct.pack('>I', h << 16))
    size = 8 + len(payload)
    return struct.pack('>I', size) + b'tkhd' + payload

# moov > trak > tkhd
def make_moov(w, h):
    tkhd = make_tkhd(w, h)
    trak_size = 8 + len(tkhd)
    trak = struct.pack('>I', trak_size) + b'trak' + tkhd
    moov_size = 8 + len(trak)
    return struct.pack('>I', moov_size) + b'moov' + trak

data = make_moov(1920, 1080)
dims = probe_mp4_dimensions(data)
assert dims == (1920, 1080), f'Expected (1920, 1080), got {dims}'
print('✓ MP4 探测正常')
"
echo

echo "5. 测试源提取..."
python3 -c "
from download_fpv import extract_sources_from_html

html = '''
var playerOptions = {
  sources: [
    { file: \"https://cdn.example.com/720p.mp4\", label: \"720p\" },
    { file: \"https://cdn.example.com/1080p.mp4\", label: \"1080p\" }
  ]
};
'''
sources = extract_sources_from_html(html)
assert len(sources) >= 2, f'Expected at least 2 sources, got {len(sources)}'
assert any(s.get('label') == '1080p' for s in sources), '未找到 1080p 源'
print('✓ 源提取正常')
"
echo

echo "6. 验证 ph_server 导入..."
python3 -c "
from ph_server import resolve_fpv_job, is_fpv_page as server_is_fpv
assert callable(resolve_fpv_job)
assert callable(server_is_fpv)
print('✓ 服务器模块导入正常')
"
echo

echo "=== 所有集成测试通过 ==="
echo
echo "注意：完整测试需要："
echo "  1. 运行 ph_server.py"
echo "  2. 访问实际 freepornvideos 页面（需代理）"
echo "  3. 验证 MP4 下载流程"
