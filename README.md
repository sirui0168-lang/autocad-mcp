# AutoCAD MCP Server & Plugin
> **专为 macOS（Apple Silicon M 系列及 Intel）及跨平台优化的 AutoCAD / DXF 矢量绘图 MCP 插件**

无需安装庞大的 AutoCAD 商业软件，即可通过本 MCP 插件借助 `ezdxf` 引擎，实现完全符合 AutoCAD 2025 / DXF 标准的工程图纸创建、编辑、图层管理、图块调用、尺寸标注与矢量/光栅图像导出。

**v1.2.0 重点：快。** 批量绘图走 `apply_ops`（加 `finish=True` 连存盘导出一起做），导出改原生 SVG 渲染器，工具全部移到工作线程——单张平面图从「几十次往返 + 7 秒导出」压到「**1 次调用 + 0.1 秒**」。

| 场景 | v1.0.0 | v1.1.0 | 提升 |
| :--- | ---: | ---: | ---: |
| 三居室平面图（35 次工具往返） | ~35 次调用 | **1 次调用**（`apply_ops(finish=True)`） | 35× |
| 首次导出 SVG（冷启动） | 7286 ms | **113 ms** | 65× |
| 5 万图元图导出 SVG | ~7000 ms | **749 ms** | 9.4× |
| 导出期间其它工具响应 | 6779 ms（事件循环被阻塞） | **0.5 ms** | 服务器不再假死 |
| 包围盒（5 万图元，缓存后） | 637 ms | **148 ms** | 4.3× |
| `tools/list` 上下文开销 | 21,681 字符 | **13,476 字符** | −38% |

---

## 目录
1. [项目结构](#项目结构)
2. [环境依赖与安装](#环境依赖与安装)
3. [快速自检与性能基准](#快速自检与性能基准)
4. [MCP 客户端接入配置](#mcp-客户端接入配置)
5. [推荐调用方式（先读这一节）](#推荐调用方式先读这一节)
6. [38 项 MCP 工具清单](#38-项-mcp-工具清单)
7. [标准图层与 ACI 颜色对照](#标准图层与-aci-颜色对照)
8. [环境变量与故障排查](#环境变量与故障排查)

---

## 项目结构

```text
autocad-mcp/
├── server.py                  # MCP Server 主入口（FastMCP，38 项工具）
├── test_server.py             # 一键自检（36 项端到端检查）
├── benchmark.py               # 性能基准（启动、导出、并发、大图）
├── requirements.txt           # Python 依赖清单 (ezdxf, pymupdf, mcp)
├── plugin.json                # Antigravity / Agent 插件元数据清单
├── mcp_config.json            # MCP 配置文件示例
├── rules/
│   └── AGENTS.md              # 规范约束（批量调用、图层命名、ACI 颜色、输出路径）
├── skills/
│   └── autocad-drawing/
│       └── SKILL.md           # 绘图业务技能指导与操作范例
└── assets/
    └── logo.svg               # 插件图标
```

---

## 环境依赖与安装

- **操作系统**：macOS (推荐 Apple Silicon M1/M2/M3/M4, 也支持 Intel)、Linux、Windows
- **Python 版本**：Python 3.10 ~ 3.12（推荐 Homebrew Python 3.12）

### 安装步骤

```bash
# 1. 进入插件目录
cd autocad-mcp

# 2. 创建并激活虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖包
pip install -r requirements.txt
```

> PNG 导出走 **PyMuPDF**：先把图纸渲染成 SVG（ezdxf 原生后端），内联掉 ezdxf 的
> CSS 类（MuPDF 不认 `<style>`，不内联会整幅变黑），再交给 MuPDF 光栅化。
> 同一张 11.7 MB、2.9 万个实体（含 8 千文本、9 千图块）的污水管网图：matplotlib
> 23.2 s → **6.7 s**；普通平立剖图 5–25 ms。再快就得上 LOD（按屏幕像素丢弃不可见文本），
> 那是另一个量级的工程，暂不做。
> `matplotlib` 只在没装 PyMuPDF 时才作为回退路径使用，两者都没有时 PNG 不可用，
> `export_svg` / `finish(svg=True)` 不受影响。

---

## 快速自检与性能基准

```bash
# 自检：36 项断言，覆盖图纸/图层/批量绘图/导出/热更新/自动归档
.venv/bin/python3 test_server.py

# 性能基准：启动耗时、批量绘图、5 万图元导出、并发不阻塞
.venv/bin/python3 benchmark.py
```

自检覆盖：
- 新建图纸、单位设置、图层创建/路由/删除（含块内实体迁移）
- `apply_ops` 批量操作：墙、门、窗、标注、填充、文字、图块、删除
- 单图元工具向后兼容（`is_closed` 别名、`save_drawing` 两种参数名）
- `export_svg` / `export_png` / `view_drawing`（内联图片）/ `finish`
- 输出目录展开 `~`、`close_drawing` 清理

---

## MCP 客户端接入配置

### 1. Antigravity IDE
在 `~/.gemini/antigravity/mcp_config.json` 或本项目的 `mcp_config.json` 中配置：

```json
{
  "mcpServers": {
    "autocad-mcp": {
      "command": "/绝对路径/autocad-mcp/.venv/bin/python3",
      "args": ["/绝对路径/autocad-mcp/server.py"],
      "env": {
        "AUTOCAD_MCP_OUTPUT": "~/Documents/AutoCAD_MCP",
        "AUTOCAD_MCP_LOG": "WARNING",
        "AUTOCAD_MCP_TOOLS": "full",
        "AUTOCAD_MCP_PNG": "auto"
      }
    }
  }
}
```

### 2. Claude Desktop
在 `~/Library/Application Support/Claude/claude_desktop_config.json` 中添加同上结构即可。

---

## 推荐调用方式（先读这一节）

> 单图元工具（`draw_line` 等）一次只能画一个对象，每个对象都是一次「模型 → 服务器 → 模型」往返。画一张平面图要几十轮，**慢的不是 Python，是轮次**。

### 一步 / 两步画完一张图

```text
apply_ops(drawing="plan", ops=[ ... 全部图元 ... ], finish=True)
# 图纸不存在会自动创建；这一次调用就写好了 DXF + SVG，整张图只有一次往返
```

需要分阶段（先看实体再出图）时，也可以拆成两步：

```text
1) apply_ops(drawing="plan", ops=[ ... ])
2) finish(drawing="plan")            # 保存 DXF + 导出 SVG
```

`apply_ops` 一次可传几十上百个 op；某一条写错只会在结果里报错，不影响其余。

```json
[
  {"op": "layer", "name": "Walls", "color": 1},
  {"op": "units", "units": "meters"},
  {"op": "wall",  "x1": 0, "y1": 0, "x2": 8, "y2": 0},
  {"op": "wall",  "x1": 8, "y1": 0, "x2": 8, "y2": 6},
  {"op": "door",  "x": 3, "y": 0, "width": 0.9},
  {"op": "window","x1": 5, "y1": 0, "x2": 6.5, "y2": 0},
  {"op": "dim",   "x1": 0, "y1": 0, "x2": 8, "y2": 0, "distance": 0.6, "layer": "Dims"},
  {"op": "text",  "text": "Living room", "x": 2, "y": 4, "height": 0.28},
  {"op": "save"}
]
```

### 支持的 op

| op | 参数 | op | 参数 |
| :--- | :--- | :--- | :--- |
| `line` | `x1,y1,x2,y2` | `mtext` | `text,x,y,width,height` |
| `circle` | `cx,cy,radius` | `dim` | `x1,y1,x2,y2,distance,angle,text_height` |
| `arc` | `cx,cy,radius,start_angle,end_angle` | `hatch` | `points,pattern,scale` |
| `rect` | `x,y,width,height` | `wall` | `x1,y1,x2,y2,thickness` |
| `polygon` | `cx,cy,radius,sides,rotation` | `door` | `x,y,width,direction` |
| `polyline` | `points,closed` | `window` | `x1,y1,x2,y2,thickness` |
| `spline` | `points,degree` | `block` | `name` |
| `ellipse` | `cx,cy,major_axis_x,major_axis_y,ratio` | `block_entity` | `block_name,entity_type,params` |
| `text` | `text,x,y,height,rotation` | `insert` | `block_name,x,y,scale,rotation` |
| `layer` | `name,color,linetype` | `delete` | `handle`（或 `index`） |
| `units` | `units` | `save` | —（存盘） |

大多数 op 还支持 `layer` 和 `color`（ACI 1–255）。

尺寸标注会自动跟着图纸单位走：`text_height` 留空时按 `$INSUNITS` 推断（毫米→2.5，米→0.25），箭头、延伸线和小数位一起缩放；`distance` 留空时偏移 3.5 倍字高，正数放在标注段的右/上方之外。

### 看图不用开文件

`view_drawing` 直接把渲染结果作为图片返回（内联），模型能当场「看」到自己画的图，省掉一次打开文件的往返。小图 ~12 ms。

---

## 38 项 MCP 工具清单

### 一、批量与复合（推荐优先使用）
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `apply_ops` | **一次调用执行多条绘图操作**（`finish=True` 顺带存盘 + 导出 SVG；图纸不存在自动创建） | `drawing`, `ops`（对象数组或 JSON 字符串）, `finish`, `create` |
| `finish` | **一次收尾：存 DXF + 导出 SVG/PNG** | `drawing`, `save`, `svg`, `png`, `dpi`, `file_path` |
| `inspect_drawing` | 一次汇总：单位、各类型图元数量、图层、块、包围盒、前 N 个图元 | `drawing`, `max_entities` |
| `view_drawing` | 渲染并**内联返回图片**，模型可直接查看 | `drawing`, `dpi` |

### 二、图纸生命周期管理
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `new_drawing` | 在内存中创建新图纸会话 | `name`, `dxf_version` (R2018/R2013/R2010 等) |
| `open_drawing` | 加载本地已有 DXF 文件 | `name`, `file_path` |
| `save_drawing` | 保存当前图纸至 DXF 文件 | `drawing`（兼容旧名 `name`）, `file_path` |
| `close_drawing`| 关闭图纸释放内存 | `name` |
| `list_drawings`| 列出当前内存中打开的所有图纸 | 无 |
| `set_units` | 设置图纸工程单位 | `drawing`, `units` (`millimeters`, `meters` 等) |

### 三、图层管理 (Layers)
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `create_layer` | 创建指定颜色与线型的新图层（线型缺失时自动降级为 Continuous 并提示） | `drawing`, `name`, `color` (ACI 1~255), `linetype` |
| `list_layers` | 列出图纸中的所有图层属性 | `drawing` |
| `delete_layer` | 删除图层，并把**全库**（含块内）实体迁到 `0` 层 | `drawing`, `layer_name` |

### 四、几何图形绘制
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `draw_line` | 绘制线段 | `drawing`, `x1, y1`, `x2, y2`, `layer`, `color` |
| `draw_circle` | 绘制圆形 | `drawing`, `cx, cy, radius`, `layer`, `color` |
| `draw_arc` | 绘制圆弧 | `drawing`, `cx, cy, radius, start_angle, end_angle`, `layer` |
| `draw_rectangle`| 绘制矩形（闭合多段线） | `drawing`, `x, y, width, height`, `layer` |
| `draw_polygon` | 绘制正多边形 | `drawing`, `cx, cy, radius, sides`, `layer` |
| `draw_polyline` | 绘制多段线 | `drawing`, `points_json`, `closed`（也接受 `is_closed`） |
| `draw_spline` | 绘制样条曲线 (B-Spline) | `drawing`, `points_json`, `degree` |
| `draw_ellipse` | 绘制椭圆 | `drawing`, `cx, cy, major_axis_x, major_axis_y, ratio`, `layer` |

### 五、建筑常用构件快捷绘制
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `draw_wall` | 绘制指定中心线与厚度的建筑墙体 | `drawing`, `x1, y1, x2, y2, thickness`, `layer` |
| `draw_door` | 绘制建筑标准平开门图例（含开启弧线） | `drawing`, `x, y, width, direction` (left/right), `layer` |
| `draw_window` | 绘制建筑标准窗图例（三线式，间距可调） | `drawing`, `x, y, width, thickness`, `layer` |

### 六、文本、标注与填充
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `add_text` | 添加单行文字 (TEXT) | `drawing`, `text, x, y, height, rotation`, `layer` |
| `add_mtext` | 添加格式化多行文字 (MTEXT) | `drawing`, `text, x, y, height, width`, `layer` |
| `add_dimension_linear` | 线性尺寸标注（严格落在指定图层；`distance`/`text_height` 为 0 时按 $INSUNITS 自动缩放） | `drawing`, `x1, y1, x2, y2, distance, angle`, `layer`, `text_height` |
| `add_hatch` | 封闭区域图案填充 | `drawing`, `pattern` (如 ANSI31/SOLID), `boundary_points_json` |

### 七、图块 (Block) 定义与复用
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `create_block` | 新建空白图块定义 | `drawing`, `block_name` |
| `add_entity_to_block` | 向图块内添加线、圆、弧等图元 | `drawing`, `block_name`, `entity_type`, `params_json` |
| `insert_block` | 实例化插入图块 | `drawing`, `block_name, insert_x, insert_y, scale_x, rotation` |
| `list_blocks` | 列出图纸中所有自定义图块 | `drawing` |

### 八、工程计算与图元分析
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `calculate_area`| 计算任意多边形闭合面积与周长 | `drawing`, `points_json` |
| `get_drawing_extents` | 获取全图实体包围盒（缓存按图元句柄复用，新增图元自动纳入计算） | `drawing` |
| `list_entities` | 遍历并查看当前模型空间图元（带 `#handle`） | `drawing`, `layer` |
| `delete_entity` | 删除图元：优先按 `handle`，也可按 `index` | `drawing`, `handle` / `index` |

### 九、可视化导出
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `export_svg` | 导出矢量图 (.svg)，**原生渲染器，不需要 matplotlib** | `drawing`, `file_path` |
| `export_png` | 导出光栅图 (.png)：SVG 经 PyMuPDF 光栅化，未装时回退 matplotlib | `drawing`, `file_path`, `dpi` |

---

## 标准图层与 ACI 颜色对照

| ACI 编号 | 常用色彩 | 推荐专业图层用途 |
| :---: | :---: | :--- |
| **1** | 红 (Red) | 承重墙体 (Walls)、危险报警线 |
| **2** | 黄 (Yellow) | 门窗构件 (Doors / Windows)、电气弱电 |
| **3** | 绿 (Green) | 给排水管线线侧标注、管道走向标号 |
| **4** | 青 (Cyan) | 给水管网 (Water Supply)、家具布置 |
| **5** | 蓝 (Blue) | 雨水管网 (Stormwater)、室外地表水系 |
| **6** | 品红 (Magenta) | **污水主管 (Sewage Main)**、强电电缆 |
| **7** | 白/黑 (White/Black)| 轴线标号、井位标号 (WS-xxxx)、图框文字 |

---

## 环境变量与故障排查

| 变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `AUTOCAD_MCP_OUTPUT` | `~/Documents/AutoCAD_MCP` | 默认输出目录，支持 `~` 展开 |
| `AUTOCAD_MCP_LOG` | `WARNING` | 日志级别；调试时设 `INFO` / `DEBUG` |
| `AUTOCAD_MCP_TOOLS` | `full` | `full` = 暴露全部 38 个工具；`core` = 只暴露 9 个复合工具（见下） |
| `AUTOCAD_MCP_PNG` | `auto` | `auto` = 有 PyMuPDF 就用它；`mupdf` / `matplotlib` 强制指定后端 |
| `AUTOCAD_MCP_WARM` | `1` | 启动时后台预热渲染器（SVG 后端；回退 matplotlib 时还有字体缓存）；设 `0` 关闭 |

- **导出很慢 / 卡住**：v1.3.0 起 PNG 走 PyMuPDF（先 SVG 再光栅化），11.7 MB 文本密集的图约 6.7 s（matplotlib 是 23 s）；普通图毫秒级。仍嫌慢就查 `AUTOCAD_MCP_PNG`：`matplotlib` 会走回退老路，`mupdf` 强制新路。文本极多的超大图本身就要数秒——ezdxf 得把每个文字转成字形轮廓。
- **调用期间整个服务器假死**：v1.1.0 已修——所有工具在工作线程执行，长任务改用共享锁，导出进行中 `list_drawings` 约 0.5 ms。
- **重启后图纸丢了**：服务器退出时会自动把改动过的图纸存到 `<输出目录>/.autosave/<name>.dxf`，用 `open_drawing` 找回。
- **stderr 满屏日志**：默认已静音，需要时用 `AUTOCAD_MCP_LOG=INFO` 打开。
- **`delete_entity` 报索引错**：改用 `handle`；`list_entities` 的输出里带 `#handle`。
