# auction-mcp

司法拍卖实时查询 MCP server — **阿里拍卖 + 京东拍卖** 双端, 纯 Python httpx, 零外部设备/桥.

## Quick start

注册到 Claude (`~/.claude.json`):
```json
{
  "mcpServers": {
    "auction": {
      "type": "stdio",
      "command": "python3",
      "args": ["/path/to/auction-mcp/server.py"]
    }
  }
}
```

```bash
pip install -r requirements.txt
pytest                  # 单元 + 容错 (零网络)
pytest --run-live       # + 集成测试 (真打 API)
```

## 工具

| 工具 | 用途 |
|---|---|
| `ali_search_judicial(province?, city?, district?, page=1)` | 阿里司法拍卖搜索, 价格降序 + 仅进行中/即将开始 |
| `ali_get_supported_areas(province?, city?)` | 列阿里支持的省/市/区县中文名 |
| `ali_get_filter_options()` | 阿里 9 个 filter 维度的完整可选项 |
| `jd_search_judicial(province?, city?, district?, page=1)` | 京东司法拍卖搜索, 价格降序 + 仅进行中/即将开始 |
| `jd_get_supported_areas(province?, city?)` | 列京东支持的省/市/区县中文名 |

地区参数全部传**中文名**, 工具自动解析两端各自的内部编码.

## 关键设计

- **阿里**: 走 `h5api.m.taobao.com` H5 mtop, sign 是公开 MD5 (`MD5(token + "&" + t + "&" + appKey + "&" + data)`). 区县编码用 **pre-2013 vintage** (阿里 server 实际接受的), bundle 在 `gb2260_200712.json`.
- **京东**: 走 `api.m.jd.com/api?functionId=getSearchData`, 无 sign 无登录. 全国 33 省 / 455 市 / 5344 区县地区树预拉到 `jd_areas.json`.
- **反爬守门**: 阿里 server 不认编码时会静默返全国乱掺数据, 工具自动校验前缀拒绝返垃圾.
- **常驻自愈**: `_m_h5_tk` cookie 过期自动重 bootstrap; baxia HTML 返结构化错误不崩.
