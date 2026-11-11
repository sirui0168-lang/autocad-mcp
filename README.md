# AutoCAD MCP Server & Plugin
> **专为 macOS（Apple Silicon M 系列及 Intel）及跨平台优化的 AutoCAD / DXF 矢量绘图 MCP 插件**

无需在本地安装庞大的 AutoCAD 商业软件，即可通过本 MCP 插件借助 `ezdxf` 引擎，实现完全符合 AutoCAD 2025 / DXF 标准的工程图纸创建、编辑、图层管理、图块调用、尺寸标注与高清图像导出。

---

## 目录
1. [项目结构](#项目结构)
2. [环境依赖与安装](#环境依赖与安装)
3. [快速自检与测试](#快速自检与测试)
4. [MCP 客户端接入配置](#mcp-客户端接入配置)
5. [34 项 MCP 核心工具清单](#34-项-mcp-核心工具清单)
6. [标准图层与 ACI 颜色对照](#标准图层与-aci-颜色对照)

---

## 项目结构

```text
autocad-mcp/
├── server.py                  # MCP Server 主入口（基于 FastMCP，34 项工程绘图与分析工具）
├── test_server.py             # 一键自检与端到端功能验证脚本
├── requirements.txt           # Python 依赖清单 (ezdxf, matplotlib, mcp)
├── plugin.json                # Antigravity / Agent 插件元数据清单
├── mcp_config.json            # MCP 配置文件示例
├── rules/
│   └── AGENTS.md              # 规范约束（图层命名、ACI 颜色标准、输出路径等）
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

---

## 快速自检与测试

安装完成后，可直接运行自带的测试脚本，一键检验所有底层组件与绘图工具：

```bash
python3 test_server.py
```

执行后将自动测试：
- 创建新图纸与设置单位 (`millimeters`)
- 创建标准图层 (`Walls`, `Doors`, `Annotations`)
- 绘制几何矩形、厚度墙体、门开启弧线、单行文本、线性标注
- 自动提取全图包围盒尺寸 (`get_drawing_extents`)
- 导出 DXF 标准文件、PNG 预览图与 SVG 矢量图

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
        "AUTOCAD_MCP_OUTPUT": "~/Documents/AutoCAD_MCP"
      }
    }
  }
}
```

### 2. Claude Desktop
在 `~/Library/Application Support/Claude/claude_desktop_config.json` 中添加同上结构即可。

---

## 34 项 MCP 核心工具清单

### 一、图纸生命周期管理
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `new_drawing` | 在内存中创建新图纸会话 | `name`, `dxf_version` (R2018/R2013/R2010等) |
| `open_drawing` | 加载本地已有 DXF 文件 | `name`, `file_path` |
| `save_drawing` | 保存当前图纸至 DXF 文件 | `name`, `file_path` (默认存至 `~/Documents/AutoCAD_MCP/`) |
| `close_drawing`| 关闭图纸释放内存 | `name` |
| `list_drawings`| 列出当前内存中打开的所有图纸 | 无 |
| `set_units` | 设置图纸工程单位 | `drawing`, `units` (`millimeters`, `meters` 等) |

### 二、图层管理 (Layers)
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `create_layer` | 创建指定颜色与线型的新图层 | `drawing`, `name`, `color` (ACI 1~255), `linetype` |
| `list_layers` | 列出图纸中的所有图层属性 | `drawing` |
| `delete_layer` | 删除空图层 | `drawing`, `layer_name` |

### 三、几何图形绘制
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `draw_line` | 绘制线段 | `drawing`, `x1, y1`, `x2, y2`, `layer`, `color` |
| `draw_circle` | 绘制圆形 | `drawing`, `cx, cy, radius`, `layer`, `color` |
| `draw_arc` | 绘制圆弧 | `drawing`, `cx, cy, radius, start_angle, end_angle`, `layer` |
| `draw_rectangle`| 绘制矩形（闭合多段线） | `drawing`, `x, y, width, height`, `layer` |
| `draw_polygon` | 绘制正多边形 | `drawing`, `cx, cy, radius, sides`, `layer` |
| `draw_polyline` | 绘制多段线 | `drawing`, `points_json` (如 `[[0,0],[10,0],[10,5]]`), `is_closed` |
| `draw_spline` | 绘制样条曲线 (B-Spline) | `drawing`, `points_json`, `degree` |
| `draw_ellipse` | 绘制椭圆 | `drawing`, `cx, cy, rx, ry, rotation` |

### 四、建筑常用构件快捷绘制
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `draw_wall` | 绘制指定中心线与厚度的建筑墙体 | `drawing`, `x1, y1, x2, y2, thickness`, `layer` |
| `draw_door` | 绘制建筑标准平开门图例（含开启弧线） | `drawing`, `x, y, width, direction` (left/right), `layer` |
| `draw_window` | 绘制建筑标准推拉/平开窗图例（四线式） | `drawing`, `x, y, width, thickness`, `layer` |

### 五、文本、标注与填充
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `add_text` | 添加单行文字 (TEXT) | `drawing`, `text, x, y, height, rotation`, `layer` |
| `add_mtext` | 添加格式化多行文字 (MTEXT) | `drawing`, `text, x, y, char_height, width`, `layer` |
| `add_dimension_linear` | 线性尺寸标注 | `drawing`, `x1, y1, x2, y2, distance, angle`, `layer` |
| `add_hatch` | 封闭区域图案填充 | `drawing`, `pattern` (如 ANSI31/SOLID), `boundary_points_json` |

### 六、图块 (Block) 定义与复用
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `create_block` | 新建空白图块定义 | `drawing`, `block_name` |
| `add_entity_to_block` | 向图块内添加线、圆、弧等图元 | `drawing`, `block_name`, `entity_type`, `params_json` |
| `insert_block` | 实例化插入图块 | `drawing`, `block_name, insert_x, insert_y, scale_x, rotation` |
| `list_blocks` | 列出图纸中所有自定义图块 | `drawing` |

### 七、工程计算与图元分析
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `calculate_area`| 计算任意多边形闭合面积与周长 | `drawing`, `points_json` |
| `get_drawing_extents` | 获取全图实体包围盒尺寸 | `drawing` |
| `list_entities` | 遍历并查看当前模型空间图元 | `drawing`, `layer` |
| `delete_entity` | 按索引精准删除图元 | `drawing`, `index` |

### 八、可视化导出
| 工具名称 | 功能描述 | 核心参数 |
| :--- | :--- | :--- |
| `export_svg` | 快速导出轻量级矢量图 (.svg) | `drawing`, `file_path` |
| `export_png` | 基于 matplotlib 导出高分辨率光栅图 (.png) | `drawing`, `file_path`, `dpi` |

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
