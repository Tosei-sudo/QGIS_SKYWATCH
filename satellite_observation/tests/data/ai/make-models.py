"""
Tiny ONNX models standing in for real ones in the AI tools' tests
(the viewer's test/ai.spec.ts, from where they are copied, and the plugin's
tests/test_qgis_ai.py). Run with `python3 make-models.py` (needs the onnx
package); the .onnx files are committed, so the tests do not need it.

- detect.onnx: a "YOLOv8" (output [1, 4 + 2, 16], Ultralytics metadata) on a
  64 × 64 input cut into 4 × 4 cells of 16 pixels; each cell is a candidate
  box of its own size, scored as class "bright" by the mean brightness of
  the cell (class "dark" scores 0).
- detectron2.onnx: the same cells as Detectron2's traced ONNX export lays
  them out: one unbatched image [3, 64, 64] of 0–255 B, G, R in; boxes
  [16, 4] (x1, y1, x2, y2), classes [16] (int64, all 0), scores [16] (the
  cell's mean brightness, 0–1) and the image size out.
- sam-encoder.onnx / sam-decoder.onnx: a "Segment Anything" pair with the
  official decoder's inputs; the low-resolution mask (256 × 256 over the
  1024 input) is a disc of radius 64 input pixels round the first point.
"""
import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper

OPSET = [helper.make_opsetid('', 17)]


def save(graph, name, metadata=None):
    model = helper.make_model(graph, opset_imports=OPSET, producer_name='browser-image-enhancement tests')
    model.ir_version = 8
    for k, v in (metadata or {}).items():
        model.metadata_props.add(key=k, value=v)
    onnx.checker.check_model(model)
    onnx.save(model, name)


def detect():
    cells = 4
    boxes = np.zeros((1, 4, cells * cells), np.float32)
    for j in range(cells):
        for i in range(cells):
            boxes[0, :, j * cells + i] = [i * 16 + 8, j * 16 + 8, 16, 16]
    nodes = [
        helper.make_node('ReduceMean', ['images'], ['gray'], axes=[1], keepdims=1),
        helper.make_node('AveragePool', ['gray'], ['pooled'], kernel_shape=[16, 16], strides=[16, 16]),
        helper.make_node('Reshape', ['pooled', 'shape'], ['bright']),
        helper.make_node('Mul', ['bright', 'zero'], ['dark']),
        helper.make_node('Concat', ['boxes', 'bright', 'dark'], ['output0'], axis=1),
    ]
    graph = helper.make_graph(
        nodes,
        'detect',
        [helper.make_tensor_value_info('images', TensorProto.FLOAT, [1, 3, 64, 64])],
        [helper.make_tensor_value_info('output0', TensorProto.FLOAT, [1, 6, cells * cells])],
        [
            numpy_helper.from_array(boxes, 'boxes'),
            numpy_helper.from_array(np.array([1, 1, cells * cells], np.int64), 'shape'),
            numpy_helper.from_array(np.zeros((1,), np.float32), 'zero'),
        ],
    )
    save(graph, 'detect.onnx', {'task': 'detect', 'imgsz': '[64, 64]', 'names': "{0: 'bright', 1: 'dark'}", 'stride': '16'})


def detectron2():
    cells = 4
    boxes = np.array([[i * 16, j * 16, i * 16 + 16, j * 16 + 16] for j in range(cells) for i in range(cells)], np.float32)
    nodes = [
        helper.make_node('ReduceMean', ['image'], ['gray'], axes=[0], keepdims=1),
        helper.make_node('Unsqueeze', ['gray', 'axis0'], ['batched']),
        helper.make_node('AveragePool', ['batched'], ['pooled'], kernel_shape=[16, 16], strides=[16, 16]),
        helper.make_node('Reshape', ['pooled', 'shape'], ['mean']),
        helper.make_node('Div', ['mean', 'full'], ['scores']),
        helper.make_node('Identity', ['cell_boxes'], ['boxes']),
        helper.make_node('Identity', ['cell_classes'], ['classes']),
        helper.make_node('Identity', ['size'], ['image_size']),
    ]
    graph = helper.make_graph(
        nodes,
        'detectron2',
        [helper.make_tensor_value_info('image', TensorProto.FLOAT, [3, 64, 64])],
        [
            helper.make_tensor_value_info('boxes', TensorProto.FLOAT, ['n', 4]),
            helper.make_tensor_value_info('classes', TensorProto.INT64, ['n']),
            helper.make_tensor_value_info('scores', TensorProto.FLOAT, ['n']),
            helper.make_tensor_value_info('image_size', TensorProto.INT64, [2]),
        ],
        [
            numpy_helper.from_array(boxes, 'cell_boxes'),
            numpy_helper.from_array(np.zeros((cells * cells,), np.int64), 'cell_classes'),
            numpy_helper.from_array(np.array([64, 64], np.int64), 'size'),
            numpy_helper.from_array(np.array([0], np.int64), 'axis0'),
            numpy_helper.from_array(np.array([cells * cells], np.int64), 'shape'),
            numpy_helper.from_array(np.array([255], np.float32), 'full'),
        ],
    )
    save(graph, 'detectron2.onnx')


def sam():
    encoder = helper.make_graph(
        [helper.make_node('AveragePool', ['image'], ['image_embeddings'], kernel_shape=[16, 16], strides=[16, 16])],
        'sam-encoder',
        [helper.make_tensor_value_info('image', TensorProto.FLOAT, [1, 3, 1024, 1024])],
        [helper.make_tensor_value_info('image_embeddings', TensorProto.FLOAT, [1, 3, 64, 64])],
    )
    save(encoder, 'sam-encoder.onnx')

    centres = (np.arange(256, dtype=np.float32) + 0.5) * 4
    nodes = [
        helper.make_node('Slice', ['point_coords', 'x0', 'x1', 'axes'], ['px3']),
        helper.make_node('Slice', ['point_coords', 'y0', 'y1', 'axes'], ['py3']),
        helper.make_node('Reshape', ['px3', 'one_one'], ['px']),
        helper.make_node('Reshape', ['py3', 'one_one'], ['py']),
        helper.make_node('Sub', ['gx', 'px'], ['dx']),
        helper.make_node('Sub', ['gy', 'py'], ['dy']),
        helper.make_node('Mul', ['dx', 'dx'], ['dx2']),
        helper.make_node('Mul', ['dy', 'dy'], ['dy2']),
        helper.make_node('Add', ['dx2', 'dy2'], ['d2']),
        helper.make_node('Sub', ['r2', 'd2'], ['logits']),
        helper.make_node('Reshape', ['logits', 'mask_shape'], ['masks']),
        helper.make_node('Identity', ['iou'], ['iou_predictions']),
    ]
    decoder = helper.make_graph(
        nodes,
        'sam-decoder',
        [
            helper.make_tensor_value_info('image_embeddings', TensorProto.FLOAT, [1, 3, 64, 64]),
            helper.make_tensor_value_info('point_coords', TensorProto.FLOAT, [1, 'n', 2]),
            helper.make_tensor_value_info('point_labels', TensorProto.FLOAT, [1, 'n']),
            helper.make_tensor_value_info('mask_input', TensorProto.FLOAT, [1, 1, 256, 256]),
            helper.make_tensor_value_info('has_mask_input', TensorProto.FLOAT, [1]),
            helper.make_tensor_value_info('orig_im_size', TensorProto.FLOAT, [2]),
        ],
        [
            helper.make_tensor_value_info('masks', TensorProto.FLOAT, [1, 1, 256, 256]),
            helper.make_tensor_value_info('iou_predictions', TensorProto.FLOAT, [1, 1]),
        ],
        [
            numpy_helper.from_array(np.array([0, 0, 0], np.int64), 'x0'),
            numpy_helper.from_array(np.array([1, 1, 1], np.int64), 'x1'),
            numpy_helper.from_array(np.array([0, 0, 1], np.int64), 'y0'),
            numpy_helper.from_array(np.array([1, 1, 2], np.int64), 'y1'),
            numpy_helper.from_array(np.array([0, 1, 2], np.int64), 'axes'),
            numpy_helper.from_array(np.array([1, 1], np.int64), 'one_one'),
            numpy_helper.from_array(centres.reshape(1, 256), 'gx'),
            numpy_helper.from_array(centres.reshape(256, 1), 'gy'),
            numpy_helper.from_array(np.array(64.0 * 64.0, np.float32), 'r2'),
            numpy_helper.from_array(np.array([1, 1, 256, 256], np.int64), 'mask_shape'),
            numpy_helper.from_array(np.array([[0.9]], np.float32), 'iou'),
        ],
    )
    save(decoder, 'sam-decoder.onnx')


detect()
detectron2()
sam()
