from flask import Flask, render_template, request, redirect
from flask_scss import Scss
from flask_sqlalchemy import SQLAlchemy



# app
app = Flask(__name__)
Scss(app)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///test_database.db"
db = SQLAlchemy(app)

class Genome(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    seq = db.Column(db.String(20), nullable=False)
    endangered = db.Column(db.Integer)
    is_edge = db.Column(db.Boolean)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/data/table/', methods=['GET','POST'])
def table():

    if request.method == 'POST':
        seq = request.form['sequencing_level']
        endangered = request.form['endangeredness_level']
        is_edge = 'edge' in request.form

        genome = Genome(seq=seq, endangered=endangered, is_edge=is_edge)

        try:
            db.session.add(genome)
            db.session.commit()
            return redirect('/data/table/')
        except Exception as e:
            print(f'error: {e}')
            return f'error: {e}'
        
    else:
        genomes = Genome.query.order_by(Genome.id).all()
        return render_template('table.html', genomes=genomes)
    

@app.route('/delete/<int:id>/')
def delete(id:int):
    delete_genome = Genome.query.get_or_404(id)
    try:
        db.session.delete(delete_genome)
        db.session.commit()
        return redirect('/data/table/')
    except Exception as e:
        print(f'error: {e}')
        return f'error: {e}'

@app.route('/update/<int:id>/', methods=['GET','POST'])
def update(id:int):
    update_genome = Genome.query.get_or_404(id)

    if request.method == 'POST':
        update_genome.seq = request.form['sequencing_level']
        update_genome.endangered = request.form['endangeredness_level']
        update_genome.is_edge = 'edge' in request.form

        try:
            db.session.commit()
            return redirect('/data/table/')
        except Exception as e:
            print(f'error: {e}')
            return f'error: {e}'

    else:
        return render_template('update.html', genome=update_genome)
    
@app.route('/data/map/')
def map():
    return render_template('map.html')





if __name__ == '__main__':
    with app.app_context():
        db.create_all()

    app.run(debug=True)