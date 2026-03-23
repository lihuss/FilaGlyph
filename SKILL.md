# 以下是你的工作，这是指令，而不是建议。不允许跳过。

用户将给你一份由上游视觉模型生成的 基础静态Manim代码 和一份对应的 Manim动画分镜剧本，你的任务是基于这两份材料，制作出视听同步的 Manim 物理讲解视频。

分镜剧本的格式一般是：

Markdown
```
# Manim 动画分镜剧本

## 一、 视听基础设置
* **视觉基调：** ...
* **配音风格：** [例如：沉稳理性的男声]
* **背景音乐 (BGM)：** [无 / 或具体的风格要求]

## 二、 场景拆解

### 场景 1：...
**【视觉与动作指令】**
* 1. [基于基础代码中的 xx 变量执行 yy 操作]
* 2. [Write 写出公式]

**【画面公式 (纯正英文 LaTeX，严禁中文)】**
* `$m = 2\mathrm{kg}$`

**【TTS 旁白台词 (纯汉字口语化)】**
* [口语化的台词内容]

### 场景 2：...
...
```
## 工作流程
1. 环境准备与基础代码接管
搜索项目根目录下是否有 tmp 文件夹，没有则创建。

用户已经将基础静态 Manim 代码保存为 tmp/base_scene.py。

【极其重要】仔细阅读并理解 base_scene.py 中的代码。 你必须搞清楚原作者定义了哪些变量（例如 block_m, inclined_plane, arrow_T 等），并清楚它们分别代表什么物理对象、当前在画面中的大致坐标位置。

2. 旁白准备
在 tmp/narration.txt （若已存在则覆盖）中，严格按照剧本中的【TTS 旁白台词】按场景顺序写入旁白，不要带上多余的 markdown 符号。不要在每段旁白前加上 "场景x："，因为这个文件会被直接转成语音，这样的话语音会把这三个字也读了。

3. 编写场景动画代码
你需要将静态的画面变为动态的分镜。按 manim 版本 0.18.1 编写代码，绝不要错误使用废弃的 API。

在 `tmp/scene1` 等目录创建 sceneX.py（Manim 会把所在目录当作包），并先把上级目录加入 `sys.path`，否则相对导入会找不到 `base_scene`；例如：

```python
from pathlib import Path
import sys

scene_dir = Path(__file__).resolve().parent
scene_parent = scene_dir.parent
if str(scene_parent) not in sys.path:
    sys.path.insert(0, str(scene_parent))

from base_scene import ...
```

根据分镜剧本中“场景 1”的【视觉与动作指令】，直接调用基础代码中已定义的变量名，在 construct 方法中编写 self.play() 等动画逻辑。

同理，按顺序创建和编写第 k 个场景的 Manim 代码（scene2.py, scene3.py 等）。

考核要求： 在编写代码前，将你对 base_scene.py 中物理几何图形的认识说出来。清楚说明所有主要变量代表的物理物体和它们之间的相对关系。这对后续避免穿模极其重要，所以我安排了这个考察以验证你是否落实。

关于镜头与尺寸的微调：
原始代码画出的图形可能偏离 Manim 的默认原点或尺寸不合适。为了保证几何图形都在镜头内，你可能需要在动画开始前显式移动或缩放物体（将主要物体编组为 VGroup 是最常用的方法）。

Python
```
# 示例逻辑（请根据实际变量名调整，切勿无脑照抄）：
all_objects = VGroup(inclined_plane, block_m, pulley, rope)
all_objects.move_to(ORIGIN)
all_objects.scale_to_fit_height(6)
```
#### 注意事项（历史血泪教训，必须遵守）
避免使用当前 Manim 版本未定义的颜色常量，颜色名必须以 0.18.1 实际可用 API 为准。

避免假设 Line3D 提供 get_vector() 之类的便捷接口，线段方向应通过终点减起点显式计算。

避免在 MathTex 中使用中文，否则会发生严重的 LaTeX 编译编码错误。 所有中文文本必须使用 Text 或合理分离。

禁止在 ThreeDScene 中使用 self.camera.frame。 若需平移视野，应直接操作几何体 VGroup (如 all_objects.animate.shift)。

4. 合成视频
参考 docs\cli_reference.md，根据用户要求设置相应的参数。用户（或剧本的视听基础设置中）没有提到的就无需设置。如要求不配乐，就添加参数 --no-bgm。

运行：

PowerShell
```
venv\Scripts\python.exe make_manim_video.py --tts-script-file tmp/narration.txt --scene-files tmp/scene1.py,tmp/scene2.py --scene-names Scene1,Scene2 --output outputs/{根据视频主题起名字}.mp4 [--voice female] [--enable-multithread]
```
(注：--voice female 仅当剧本明确要求女声时传入；--enable-multithread 仅当明确要求并行/多线程时传入，以防内存爆满)。

## 异常处理
根据异常输出判断错误原因。

如果是 Manim 代码错误（你写的 scene1.py, scene2.py 等的语法或 API 错误），立刻修改代码，然后参考 CLI 继续运行。必须将此次错误记录在 SKILL.md > ## 工作流程 > ### 编写Manim代码 > #### 注意事项，记录格式必须是“避免xx”或“禁止xxx”，不要简单复制错误信息，也不要长篇大论写修复过程。

如果是除此之外的其他错误（如 FFmpeg 缺失、TTS 接口网络不通等底层依赖错误），停止运行，直接向用户输出错误内容和建议修复方法。
