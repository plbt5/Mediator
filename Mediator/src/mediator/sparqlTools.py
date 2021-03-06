'''
Created on 22 mrt. 2016

@author: brandtp

A sparql-query is structured as follows:

    Prefix Declarations:
        BASE
        PREFIX
    Dataset Definition:
        FROM
        FROM NAMED
    Result Clause:
        SELECT
        CONSTRUCT
        DESCRIBE
        ASK
    Query Pattern:
        WHERE
    Query Modifiers:
        ORDER BY
        LIMIT
        OFFSET
        DISTINCT
        REDUCED

This module provides tools to relate the various elements of a query to each other, as well as to the EDOAL correspondences.
'''

from parsertools.base import ParseStruct
from parsertools.parsers.sparqlparser import SPARQLParser
import warnings
from builtins import str
from utilities.namespaces import NSManager  
from utilities import utils      
from mediator import mediatorTools
import json
from os import linesep
from json.decoder import JSONDecodeError
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError
from collections import namedtuple


def determineBGPPosition(node=None):
    '''
    Identify the BGP position of a ParseStruct Node by searching the ancestor tree.
    returns: (String, canonical as Context.localLabels): the Basic Graph Pattern position of this node: <S|P|O>,
             or None when the position cannot be determined, e.g., when the node is not part of a query pattern triple.
    '''
    # Since we go upwards, we first need to skip all non-branching intermediate nodes. Then, all branching nodes are processed
    # from lower to higher. 
    assert isinstance(node, ParseStruct), "determineBGPPosition: illegal input data, <class ParseStruct> expected, got {}".format(type(node))
    bgpPos = None
    ancestors = [node] + node.getAncestors()
    for ancestor in ancestors:
        # The stop criterion for this loop is having found the BGP position that this QPTriplenode is about.
        nType = type(ancestor).__name__
#         print("> found ancestor '{}' ({})".format(nType, ancestor))
        if not ancestor.isBranch():
            # This is ancestor is not a branching node. Skip to the next ancestor but remember this node since, 
            # eventually, this will be the top node of this branch
            topOfBranch = ancestor
            continue
        elif nType in ['ObjectList', 'ObjectListPath']:
            # This Node represents an object
            bgpPos = "object"
        elif nType in ['VerbSimple', 'VerbPath']:
            # This Node represents a property
            bgpPos = "property"
        elif nType == 'TriplesSameSubjectPath':
            # We are in a TSSP leg, hence the main Node represents a subject
            # and its children represent the rest of the triple.
            bgpPos = "subject"
        elif nType == 'PropertyListPathNotEmpty':
            # We are in a PLPNE leg, WITHOUT bgpPos assigned already hence determine
            # whether the self node represents either a property or an object
            if type(topOfBranch).__name__ in ['ObjectListPath', 'ObjectList']:
                bgpPos='object'
            elif type(topOfBranch).__name__ in ['VerbPath', 'VerbSimple']:
                # Now in the branch leading to itself 
                bgpPos="property"
            else: raise RuntimeError("determineBGPPosition: Unexpectedly found [{}] ([{}]) as child of [{}] ([{}]). Please file BUG report.".format(topOfBranch, type(topOfBranch).__name__,ancestor,type(ancestor).__name__))
        elif nType == 'TriplesBlock':
            RuntimeError('determineBGPPosition: We assumed this to be dead code, because the Query Pattern should had been processed by now. Found <{}> as ancestor to <{}>. Please file BUG report.'.format(nType, ancestor))
        else: RuntimeError("determineBGPPosition: Unexpectedly found <{}> as ancestor to <{}>. This might be valid, but requires further study. Please file BUG report.".format(nType, ancestor))
        break
    if not bgpPos:
        warnings.warn("determineBGPPosition: Couldn't determine BGP position for '{}'. Please file BUG report.".format(node))
        return None
    return Context.localLabels[bgpPos]

QueryForm = namedtuple('QueryForm', ['select', 'ask', 'construct', 'describe'])
queryForm = QueryForm('SELECT', 'ASK', 'CONSTRUCT', 'DESCRIBE')
SparqlType = namedtuple('SparqlType', ['query', 'result_set', 'result_graph'])
sparqlType = SparqlType('QUERY', 'RESULT_SET', 'RESULT_GRAPH')

class SparqlData():
    '''
    This represents the parent class for any sparql data that is to be mediated and communicated.  
    '''
    def __init__(self):
        self.parsed = None
        self.type = None

def isSparqlResult(data=None):  
    '''
    Test, in a naive way, whether the data represents a sparql query response. Naive, since when parsing passes the first line
    it is considered so.
    Input: an xml formatted string, or a json formatted dict. Any other format will return False
    '''
    assert data and data != '', "Cannot determine type of data without data"
    if isinstance(data, dict):
        # Assume JSON format or regular dict format
        return "head" in data
    elif isinstance(data, str):
        # Assume XML format
        try:
            data = ' '.join(data.split()) # remove white spaces
            data = linesep.join([s for s in data.splitlines() if s]) # remove empty lines
            root = ElementTree.fromstring(data)
            return root.tag == '{http://www.w3.org/2005/sparql-results#}sparql'
        except Exception: pass
    return False
def isSparqlQuery(data=None):
    '''
    Test, in a naive way, whether the data represents a sparql query. Naive, since when parsing passes the first line
    it is considered a sparql query
    '''
    assert data and data != '', "Cannot determine type of data without data"
    if isinstance(data, str):
        try:
            for line in data.splitlines():
                line = ' '.join(line.split()) # remove white spaces
                if len(line) != 0 and line[0] != "#":
                    word = line.split()[0]
                    return word in ["PREFIX", "BASE", "SELECT", "CONSTRUCT", "DESCRIBE", "ASK"]
            return False
        except Exception: pass
    return False
    
class Context():
    '''
    Represents the sparql context of a single (source) EntityExpression that is mentioned in the EDOAL correspondence.
    It builds the relationship between:
    1 - the source <EntityExpression> this is about;
    2 - the <entity>'s that are involved in the EntityExpression, and for *each* of these 
        1 - the triple(s) in the Query Pattern of the sparql tree that mention the EntityIri, and 
        2 - the restrictions that yield in the Query Modifiers of the sparql tree for this entity.
    '''
    
    mns = 'http://ts.tno.nl/mediator/1.0/'
    localLabels = {
            'appearsAs' : 'AppearsAs',
            'binds'     : 'BINDS',
            'represents': 'REPR',
            'object'    : 'OBJ',
            'property'  : 'PROP',
            'subject'   : 'SUBJ',
            'criterion' : 'CRTN',
            'operation' : 'OPRTN',
            'limit'     : 'LMT',
            'binding'   : 'BNDNG'
            }


    class QueryPatternTripleAssociation():
        '''
        A QPTAssoc contains the association between one Correspondence of an Alignment and: (i) the BGP patterns (as references, i.e., the QPTripleRef) 
        that are referred to by the correspondence; and (ii) the value logic patterns that are related to it through shared query variables. 
        The BGP patterns are part of the WHERE clause, while the value logic patterns are part of the FILTER clause.
        '''
        
        def __init__(self, *, entity, sparql_tree, nsMgr):
            self.represents = '' # (Entity) the EDOAL entity_iri (Class, Property, Relation, Instance) name;
                                 # This is in fact unnecessary because this is already stored in the higher Context class
            self.qptRefs = []    # List of (QPTripleRef)s, i.e., Query Pattern nodes that address the Entity
            self.pfdNodes = {}   # Temporary namespace dictionary. Dict of (ParseStruct)s indexed by prefix : the PrefixDecl nodes that this entity_iri relates to
            self.sparqlTree = '' # The sparql tree that this class uses to relate to
            
            assert isinstance(entity, mediatorTools._Entity)
            assert isinstance(nsMgr, NSManager)
            assert isinstance(sparql_tree, ParseStruct)
            # Now find the atom of the main Node, i.e., what this is all about, and store it
            if entity == None or sparql_tree == None:
                raise RuntimeError("Require parsed sparql tree and sparql node, and edoal entity_iri expression.")
#             print("context: entity expression {}".format(entity))
            self.represents = entity
            self.sparqlTree = sparql_tree
            # Now find the [PrefixDecl] nodes
            #TODO: Remove this after refactoring to locally valid namespace expansion etc. in SPARQLStruct
            prefixDecls = sparql_tree.searchElements(element_type=SPARQLParser.PrefixDecl)
            _, src_iriref, _ = nsMgr.splitIri(entity.getIriRef())
            for prefixDecl in prefixDecls:
                ns_prefix, ns_iriref = str(prefixDecl.prefix)[:-1], str(prefixDecl.namespace)[1:-1]
                if ns_iriref == src_iriref: 
                    self.pfdNodes[ns_prefix] = {}
                    self.pfdNodes[ns_prefix]['ns_iriref'] = '<' + ns_iriref + '>'
                    self.pfdNodes[ns_prefix]['node'] = prefixDecl
                    
        class QPTripleRef():
            '''
            A QPTripleRef represents a single query pattern, i.e., an {s,p,o} BGP triple, which is referred to by an entity that is mentioned in a Correspondence.  
            To that end it contains (i) the node in the parsed sparql tree that represents the edoal entity, (ii) its position in the triple, (iii) the nodes that form the triple, including their position in the triple, and (iv) the variables that are bound in this triples, if any.
            Indeed, (i) and (ii) are repeated in (iii), however, (i) and (ii) underline its function as entity.
            '''
            #TODO: Extend this structure to enable the inclusion of more than one node as vehicle for an edoal entity. The current implementation only allows a triple to contain one edoal entity, which is an unnecessary limitation
            def __init__(self, about=None, bgp_type=None):
                '''
                Initialize a triple object. 
                Input: None.
                '''
                self.referred = None      # (ParseInfo): the atomic node in the sparql tree that is referred to by the Edoal Entity Element.
                self.translated = False   # (Boolean): keeps track whether the referred node has already been translated or not. A node can only be translated once.
                self.type = ''       # (String, canonical as Context.localLabels): the Basic Graph Pattern position that the entity is about: <S|P|O>.
                self.binds = set()   # (List of String)(optional): a list of names of the Vars that are bound in this triple. 
                self.associates = {} # (Dict{S|P|O, ParseInfo}): the annotated (s,p,o) triple, each of which refers to a QPNode in the triple and is annotated with its BGP position.
                self.partOfRDF = ''  #TODO: The RDF Triple Pattern (s,p,o) (https://www.w3.org/TR/2013/REC-sparql11-query-20130321/#defn_TriplePattern)
                                     #     that this node is part of (created)
                #TODO: register our own namespace for mediator, and use its prefix to local labels, in order to prevent label mangling
#                 if about == None: raise RuntimeError("QPTripleRef.init: Cannot create QPTripleRef from None")
#                 atom = about.descend()
#                 if atom.isAtom():
#                     self.about = atom
#                     self.setType(bgp_type)
#                 else: raise NotImplementedError("QPTripleRef.init: Creating QPTripleRef from non-atom node ({}) is not implemented, and considered bad practice.".format(atom))
            
            def addReferred(self, referred=None, bgp_type=None):
                '''
                A triple is being referred to by an entity. The entity refers a node in that triple. Include that referred node and annotate its BGP position.
                Input:
                    * referred (ParseStruct):    The node in the graph that is either an atom or can be traced to an atom, representing the entity element of interest.
                    * bgp_type (['subject','property','object']): The BGP position of the node in the triple
                Return: True when the triple is complete, False otherwise
                '''
                assert not (bgp_type == None or referred == None), "QPTripleRef.addReferred: Require node and its BGPpos that is being referred to by the entity. Please report this BUG."
                atom = referred.descend()
                if atom.isAtom():
                    self.referred = atom
                else: raise NotImplementedError("QPTripleRef.addReferred: Node that is being referred to by entity is a non-atom node ({}), and considered bad practice. Please report this BUG.".format(atom))
                if bgp_type in ['subject','property','object']:
                    self.type = Context.localLabels[bgp_type]
                elif bgp_type in [Context.localLabels['subject'], Context.localLabels['property'], Context.localLabels['object']]:
                    self.type = bgp_type
                else:
                    raise AttributeError("QPTripleRef.addReferred: Expected a BGP position, but got {}. Please report this BUG".format(bgp_type))
                return(self.addAssociate(bgp_type=bgp_type, assoc_node=atom))

            def addAssociate(self, bgp_type=None, assoc_node=None):
                '''
                1 - This method adds to its subject QPTripleRef this (associated) node that is part of the QPTripleRef's BGP (s,p,o). To that end it will find 
                the terminal or atom node in this branch, and raise an error when there are more branches down the tree. 
                2 - In conjunction to this, it also checks whether the associated node represents a sparql variable. If so, it will add the 
                variable to the binding of its subject QPTripleRef.
                3 - TODO: when the BGP becomes complete after this last addition, it will generate an RDF triple from it and store it in its
                subject QPTripleRef (not a triple store).
                Input: 
                * assoc_node:   The associated node, which is considered to be on a tree vertice that won't split into other branches. The method will throw a Runtime error
                                when there are branches found lower in the tree.
                * bgp_type:     The position of the association node in the BGP triple. This can be either a string ('subject' | 'property' | 'object'), or
                                a full blown URIRef of identical nature.
                Return:
                * True:     When the QPTriple is complete, i.e., when the BGP (s,p,o) is all filled
                * False:    Otherwise.
                '''
                # First figure out the bgp type
                assert bgp_type != None and assoc_node != None, 'Cannot add empty querypattern qptRefs'
                if bgp_type in ['subject','property','object']: 
                    bgp_position = Context.localLabels[bgp_type]
                elif bgp_type in [Context.localLabels['subject'], Context.localLabels['property'], Context.localLabels['object']]:
                    bgp_position = bgp_type
                else:
                    raise AttributeError("Expected a BGP position, but got {}".format(bgp_type))
                
                # Now find the terminal/atom of the association node
                atom = assoc_node.descend()
                if atom.isAtom():
                    # Associate it with the main Node
                    if bgp_position in self.associates:
                        raise RuntimeError('Position of <{}> in {} already taken'.format(bgp_position, str(self)))
                    else: 
                        self.associates[bgp_position] = atom
                    # In addition, this might represent a variable, hence consider to store the binding
                    self.considerBinding(atom)
                else: raise NotImplementedError("Sparql node unexpectedly appears parent of more than one atomic path. Found <{}> with siblings.".format(atom))
                
#                 print('[{}] determined as <{}> associate to <{}>'.format(str(assoc_node),bgp_position,str(self.referred)))

                # Lastly, when all three nodes are added, generate an RDF triple and store it in its subject QPTripleRef
                if len(self.associates) == 3:
                    #TODO: create three RDF Terms (each as Literal, URIRef or BNode), and formulate & store it as statement
                    self.partOfRDF = (self.associates[Context.localLabels['subject']], self.associates[Context.localLabels['property']], self.associates[Context.localLabels['object']])
                    return True
                else: return False
            
            def considerBinding(self, qpnode):
                # We assume that we only need to take into consideration the variables in order to be able to follow through with the translations.
                # Note that the binds-attribute is a set, hence no double entries of variables
                if type(qpnode).__name__ in ['VAR1', 'VAR2']:
                    if not qpnode == self.referred:
                        self.binds.add(str(qpnode))
            
            def replaceWith(self, tgtIri=None):
                '''
                Replace the IRI in the referred node with the target IRI. This is an in-line replacement.
                Input: tgtIri (string), representing a valid IRI.
                return: True on successful replacement; False otherwise, indicating:
                 * the triple had been translated already
                '''
                assert NSManager.isIRI(tgtIri), "QPTTripleRef(): Cannot exchange a node without a proper target IRI, got '{}'".format(tgtIri)
                if not self.translated:
                    self.referred.updateWith(tgtIri)
                    self.translated = True
                    return True
                return False
            
            def __repr__(self):
                result = "node:\n\tof type   : " + str(self.type) + "\n\tabout     : " + str(self.referred)
                for a in self.associates:
                    result += "\n\tassociates: " + str(self.associates[a]) + " ( " + str(a) + " )"
                for b in self.binds:
                    result += "\n\tbinds     : " + str(b)
                return(result) 
            
            def __str__(self):
                result = str(self.type) + ' in BGP(' 
                if Context.localLabels['subject'] in self.associates:
                    result += str(self.associates[Context.localLabels['subject']])
                else: result += str(None)
                if Context.localLabels['property'] in self.associates:
                    result += ', ' + str(self.associates[Context.localLabels['property']])
                else: result += ', ' + str(None)
                if Context.localLabels['object'] in self.associates:
                    result += ', ' + str(self.associates[Context.localLabels['object']]) + ')'
                else: result += ', ' + str(None) + ')'
                return(result)
        
        def getTriples(self, rq_node=None, referred=None):
            '''
            For query nodes that are part of the sparql query pattern, get the triples or triple parts that this node is part of.
            Input: 
              * rq_node (ParseStruct) - the node that represents the top of the graph in which triple parts are sought. 
              * referred (['subject','property','object']) - the position that is being referred in the triples
            Return: (list of QPTripleRef) - the (possibly) incomplete objects, each of which represents the (part of the) triple that 
                the current node is part of.
            '''
            # The BGP's can share nodes through the use of ';'and ',', hence first consider the type of the node:
            # Subjects - can be shared over different Properties
            # Properties - can be shared over different Objects
            # Objects - can not be shared
            #
            # Principle of operation:
            # The graph is ordered as one S that can have multiple P branches that can have multiple O branches. Apply a recursive pattern,
            # by deferring the start of the creation of a BGP triple from the (underlying) Object, since each unique object identifies one
            # unique triple. Then, progressing upwards in the branch from the object, add the shared property and shared subject.
            # Therefore, 
            # 1 - for each subject, get from its underlying properties their BGP triples, and share the subject node with them;
            # 2 - for each property, get from its underlying objects their BGP triples, and share the property node with them;
            # 3 - for each object, create a triple, add the object node to it.
            
            assert isinstance(rq_node, ParseStruct) and referred in [Context.localLabels['subject'], Context.localLabels['property'], Context.localLabels['object']], \
                "Context...getTriples(): illegal input ('{}', '{}'). Please file a BUG report".format(rq_node, referred)
            qptList = []
            done = False
            
            # Skip the non-branching, intermediate levels. Go up to the node below lowest branching node, i.e., where 
            # the parent is branching.
            # However, do NOT fail to notice a branch that represents an Object node, since that is not a branching node but
            # should be used as the start of the triple!
            nType = type(rq_node).__name__
            while not (rq_node.getParent().isBranch() or nType in ['ObjectPath', 'Object']):
                rq_node = rq_node.getParent()
                nType = type(rq_node).__name__
            
#             print("Context...getTriples(): at node {}".format(repr(rq_node)))
            if nType == 'TriplesSameSubjectPath':
                # Node has one pair of (VarOrTerm, PropertyListPathNotEmpty).
                # 1 - Get this pair and:
                # 2 - get the property triples
                # 3 - add to each triple the shared subject from the VarOrTerm, either as being referred to, or mere as associate of
                #
                #TODO: SPARQL 1.1 - [TSSP] ::= VOT PPLPNE | TriplesNodePath PropertyListPath. We only handled the first possibility.
                for vot, plpne, period in utils.grouper(rq_node.getChildren(), 3, "."):
                    assert type(vot).__name__ == "VarOrTerm" and type(plpne).__name__ == 'PropertyListPathNotEmpty' and str(period) == ".", \
                        "Context...getTriples(): ('VarOrTerm', 'PropertyListPathNotEmpty', '.') tuple expected, got ('{}', '{}', '{}'). Please file BUG report.".format(vot, plpne, period)
                    triples = self.getTriples(rq_node=plpne, referred=referred)
                    done = True
                    if referred == Context.localLabels['subject']:
                        # This represents the referred node. Hence add the node as being referred by the entity
                        for triple in triples:
                            done = triple.addReferred(referred=vot, bgp_type="subject") and done
                    else:
                        # Add the node as mere associate to this triple
                        for triple in triples:
                            done = triple.addAssociate(bgp_type="subject", assoc_node=vot) and done
                    if not done: raise RuntimeError("Context...getTriples(): Triple SHOULD be complete when adding subject '{}'. Please file a BUG".format(rq_node))
                    qptList.extend(triples)
            
            elif nType == "VarOrTerm":
                # Node has only a VarOrTerm descendant. That means that the search for triples started at a Subject node, i.e., somewhere in this 
                #     downward branch up to and including this very node. That means that this node is the referred node, i.e., it bears the entity. 
                #     Hence, creation of a triple is due, which should start at the parent, i.e., the TSSP. Since recurrence goes down, this will 
                #     never happen unless we force recurrence here to start from the upper TSSP. 
                if referred == Context.localLabels['subject']:
                    # This represents the referred node indeed, indicating the search for triples just started and recursion did not yet happen. 
                    # Hence start a recursion from the upper TSSP.
                    qptList = self.getTriples(rq_node=rq_node.getParent(), referred=referred)
                else:
                    # Since this does not represent the referred node, we are in a recursion already. This is mere impossible, since the recursion
                    # is downwards and the Subject represents the most upward node. Therefore, this is a mere failure.
                    raise RuntimeError("Context...getTriples(): Fatal - Looking for triples that share '{}' node in a '{}' node should be impossible.".format(referred, rq_node))
            elif nType == "PropertyListPathNotEmpty":
                # Node has at least one pair of ([VerbPath | VerbSimple], [ObjectListPath | ObjectList]). 
                # 1 - Get each pair and:
                # 2 - get the object triples
                # 3 - add to each triple the shared property from the VerbPath, either as being referred to, or mere as associate of
                for verb, obj, semicol in utils.grouper(rq_node.getChildren(), 3, ";"):
                    assert type(verb).__name__ in ['VerbPath', 'VerbSimple'] and type(obj).__name__ in ['ObjectListPath', 'ObjectList'] and str(semicol) == ";", \
                        "Context...getTriples(): (['VerbPath' | 'VerbSimple'], ['ObjectListPath' | 'ObjectList'], ';') tuple expected, got ('{}', '{}', '{}'). Please file BUG report.".format(repr(verb), repr(obj), repr(semicol))
                    triples = self.getTriples(rq_node=obj, referred=referred)
                    if referred == Context.localLabels['property']:
                        # This represents the referred node. Hence add the node as being referred by the entity
                        for triple in triples:
                            done = triple.addReferred(referred=verb, bgp_type=referred) or done
                    else:
                        # Add the node as mere associate to this triple
                        for triple in triples:
                            done = triple.addAssociate(bgp_type="property", assoc_node=verb) or done
                    if done: raise RuntimeError("Context...getTriples(): Triple CANNOT be complete yet when adding property '{}'. Please file a BUG".format(rq_node))
                    qptList.extend(triples)

            elif nType in ['VerbSimple', 'VerbPath']:
                # Node has only a Property descendant. Two possibilities why we are here:
                # 1 - The search for triples started at a Property node, i.e., somewhere in this downward branch up to and including
                #     this very node. That means that this node is the referred node, i.e., it bears the entity. Hence, creation of a 
                #     triple is due, which should start at the parent, i.e., the PLPNE. Since recurrence goes down, this will never happen 
                #     unless we force recurrence here to start from the upper PLPNE. 
                # 2 - The search for triples started upwards, and we got recursively downwards here. IN that case there are no triple parts 
                #     down this lane, since the atomic property down here should have been addressed already during node [PropertyListPathNotEmpty].
                #     In this case, this node should be ignored.
                # We can discern case 1 from case 2, since case 1 bears Property as being the referred node.
                if referred == Context.localLabels['property']:
                    # This represents the referred node, i.e., case 1. Hence start a recursion from the upper PLPNE.
                    qptList = self.getTriples(rq_node=rq_node.getParent(), referred=referred)
                else:
                    warnings.warn("Context...getTriples(): Looking for triples in a '{}' node is irrelevant. Ignoring node '{}'.".format(nType, rq_node))
            elif nType in ['ObjectList', 'ObjectListPath']:
                # Node has potentially more than one object that share the property. Hence:
                # 1 - get triples for each object, and extend the return list with it.
                for objNode1, objNode2 in utils.grouper(rq_node.getChildren(), 2, ","):
                    # Since the "," is not a typed node, we need to go around this and test if there actually *is* a second object(path). 
                    assert type(objNode1).__name__ in ['Object', 'ObjectPath'], "Context...getTriples(): (['Object' | 'ObjectPath'], ',') pair expected, got ('{}', '{}'). Please file BUG report.".format(objNode1, objNode2)
                    qptList.extend(self.getTriples(rq_node=objNode1, referred=referred))
                    if type(objNode2).__name__ in ['Object', 'ObjectPath']:
                        qptList.extend(self.getTriples(rq_node=objNode2, referred=referred))
                    elif objNode2 != ",":
                        raise AssertionError("Context...getTriples(): (['Object' | 'ObjectPath'], ',') pair expected, got ('{}', '{}'). Please file BUG report.".format(objNode1, objNode2))
                        
            elif nType in ['ObjectPath', 'Object']:
                # Node has only an Object descendant, hence:
                # 1 - create a QPTripleRef
                # 2 - add the (object) node, either as being referred to, or mere as associate of
                # 3 - add it to the return list
                QPTriple = self.QPTripleRef()
                if referred == Context.localLabels['object']:
                    # This represents the referred node. Hence add the node as being referred by the entity
                    done = QPTriple.addReferred(referred=rq_node, bgp_type="object")
                else:
                    # Add the node as mere associate to this triple
                    done = QPTriple.addAssociate(bgp_type="object", assoc_node=rq_node)
                if done: raise RuntimeError("Context...getTriples(): Triple CANNOT be complete yet when adding object '{}'. Please file a BUG".format(rq_node))
                qptList.append(QPTriple)
            
            else:
                raise AttributeError("Did not expect node '{}' ({}) while adding triples. Please file BUG report".format(rq_node, nType))
            # Done with finding triples for this node, hence return the found triple parts
            return qptList
        
        def addQPTRefs(self, rq_node=None):
            """
            A query node that is part of the WHERE or ASK clause, is part of at least one and possibly (depending on the use of ';' and ',') more BGP triples.
            This function adds QPTripleRef's, each of one representing a single complete BGP triple in the parsed query that the input node is part of.
            Input: rq_node (ParseStruct) - the query node, representing one of {s, p, o}
            """
            #
            # Consider that this method is called with the query node in the graph that represents the entity for which a context is due. This method 
            # will need to find all triples that this node is part of. It has to look in two directions for that; up and down. Downwards are all 
            # parts (nodes) of triples that share this node; Upwards are the nodes that are shared by all downwards triple parts. Moreover, these upwards node(s) 
            # complete these triple parts. 
            # * In upwards direction, for an Object entity, it will find two nodes to complete the triple: the property 
            #   and the subject nodes. For a Property entity, it will only need one node for the triple to complete, the Subject node. For a Subject 
            #   node, there are no upward nodes to be found.
            # * In downwards direction, a number of nodes can be found. A subject can be shared by many triples (using the ';' notation). A Property can be shared by
            #   less, but still many triples (using the ','notation). An object is just one node. Each object indicates a unique triple.
            # * The graph has a depth of three levels, in principle: the Subject level, Property level and Object level. However, due to the many variations 
            #   that are allowed for sparql queries, each graph has several intermediate levels between the three. However, the query node that the method is 
            #   entered with, is always located in a non-branching graph part, i.e., a straight, non-branching path between the query node and the atom.
            #   The intermediate nodes between the query node and its atom are irrelevant, and can be skipped. In upwards direction, the only interesting nodes are 
            #   the nodes that branch downwards. Hence, the intermediate nodes in upwards direction are irrelevant as well, and are skipped too.
            #
            # Principle of operation:
            # 1 - Determine the atom node for the query node, i.e., skip several intermediate nodes downward; this is the node that the entity refers to.
            # 2 - Determine the BGP position of the query node.
            # 3 - Find all (incomplete) triples that share this node, i.e., a downwards direction.
            # 4 - Then, go upwards from the query node to the subject node in a straight line to find either the (Property, Subject) nodes or 
            #     the single (Subject) node that complete the found triples.
            # 5 - Finally, add the triples to the QPTAssociation object
            #
            assert isinstance(rq_node, ParseStruct)
            
            # 1 - Determine the atom node for the query node, i.e., skip several intermediate nodes downward; this is the node that the entity refers to.
            referredNode = rq_node.descend()
            # A referred node MUST be an atom, because an entity always represents a single BGP position as opposed to a triple (part). 
            assert referredNode.isAtom(), "Context...addQPTRefs(): Fatal - impossible to have an entity NOT to refer to a single BGP position, but found node '{}'".format(repr(referredNode))

            # 2 - Determine the BGP position of the query node.
            bgpPos = determineBGPPosition(rq_node)

#             print("Context...addQPTRefs(): Looking for triples that share the '{}' position of node '{}'.".format(bgpPos, rq_node))
            
            # 3 - Find all (incomplete) triples that share this node, i.e., a downwards direction.
            # 3.1 - If the node represents an Object, there are no other triples that share this node. In stead, this *is* the triple.
            if bgpPos == Context.localLabels["object"]:
                triples = self.getTriples(rq_node=rq_node, referred=bgpPos)
            else:
                # 3.2 - If the node represents a Property or Subject, the triples that share this node, are to be found in the sibling branches. 
                # 3.2.1 - Hence, first find the branching parent of me and my siblings.
#                 print("having siblings: at ", repr(rq_node))
                while not rq_node.getParent().isBranch():
                    rq_node = rq_node.getParent()
#                     print("having siblings: at ", repr(rq_node))
                # 3.2.2 - Found my parent, but it can have more groups of sibling branches. Get the correct group, i.e., the group that the referred node had its branch in.
                #        In that group, select my sibling (we assume groups of two branches only, since we are interested in PLPNE and TSSP only)
                siblings = rq_node
                for branch1, branch2, _ in utils.grouper(rq_node.getParent().getChildren(), 3, "?"):
#                     print(repr(branch1), repr(branch2))
                    if rq_node == branch1:
                        siblings = branch2
                        break
                    elif rq_node == branch2:
                        siblings = branch1
                        break
#                 print("Finding triples in ", repr(siblings))
                triples = self.getTriples(rq_node=siblings, referred=bgpPos)
                
#             print("Context...addQPTRefs(): Found {} triples: {}".format(len(triples), triples))
            # 4 - Then, go upwards from the query node to the subject node in a straight line to find 
            #     either the Property and Subject nodes, or the Subject node only, to complete the triple.
            
            ancestors = [rq_node] + rq_node.getAncestors()
            for child, parent in utils.pairwise(ancestors):
                # A node of type "TriplesBlock" indicates the root of the triples, and
                # hence acts as the stop criterion.
                if type(parent).__name__ == "TriplesBlock":
                    break
                elif parent.isBranch():
                    # Skip the intermediary nodes, only process the branching nodes
                    
                    # A node of type "TriplesSameSubjectPath" indicates a Subject. Hence share this node / complete the triples.
                    if type(parent).__name__ == 'TriplesSameSubjectPath':
                        # Get the subject node, i.e., the VarOrTerm node
                        children = parent.getChildren()
                        for vot, _, _ in utils.grouper(children, 3, "."):
                            # 1 - Associate the Subject node with the triples, unless 
                            #     - the Subject node is the referred node, because it is already done by addReferred() in an earlier state
                            #TODO: Code Smell - Because addReferred also calls addAssociate, we now end up with an additional IF statement to check if it is done or not
                            # 2 - Since this Subject nodes should complete the triples, check if that is so.
                            done = True
                            for triple in triples:
#                                 if bgpPos != Context.localLabels["subject"]:
#                                 print("Context...addQPTRefs(): Make Subject Association for: {}".format(triple))
                                done = triple.addAssociate(bgp_type="subject", assoc_node=vot.descend()) and done
#                                 print("\tNode status: {}".format(triple))
                            if not done:
                                raise RuntimeError("Context...addQPTRefs(): Triples SHOULD ALL be complete when associating the subject '{}'. Please file a BUG".format(vot))
                        
                    # A node of type "PropertyListPathNotEmpty" indicates a Property. Hence share this node / complete the triples.
                    elif type(parent).__name__ == 'PropertyListPathNotEmpty':
                        # Get the property node
                        for verb, obj, _ in utils.grouper(parent.getChildren(), 3, ";"):
                            if child == obj:
                                # The triples originated from this object(s) branch, hence:
                                # 1 - Associate the Property node with the triples, unless 
                                #     - the Property node is the referred node, because then it is already done by addReferred() in an earlier state
                                #TODO: Code Smell - Because addReferred also calls addAssociate, we now end up with an additional IF statement to check if it is done or not
                                # 2 - Since this Property nodes should NOT complete the triples, check if that is so.
                                done = False
                                for triple in triples:
                                    if bgpPos != Context.localLabels["property"]:
#                                         print("Context...addQPTRefs(): Make Property Association for: {}".format(triple))
                                        done = triple.addAssociate(bgp_type="property", assoc_node=verb) or done
                                    else:
                                        warnings.warn("Context...addQPTRefs(): Has Property Node already been associated with this triple: {}".format(repr(triple)))
                                if done:
                                    raise RuntimeError("Context...addQPTRefs(): Triples CANNOT be complete already when associating the property '{}'. Please file a BUG".format(verb))
                            elif child == verb:
                                # The triples originated from this property/properties branch, hence:
                                # 1 - Associate the Property node as referred node with the triples
                                # 2 - Since this Property nodes should NOT complete the triples, check if that is so.
                                done = False
                                for triple in triples:
#                                     print("Context...addQPTRefs(): Make Property Association for: {}".format(triple))
                                    done = triple.addReferred(bgp_type="property", referred=verb) or done
                                if done:
                                    raise RuntimeError("Context...addQPTRefs(): Triples CANNOT be complete already when associating the property '{}'. Please file a BUG".format(verb))
                                

            # 5 - Add the triples to the QPTAssociation object
            self.qptRefs.extend(triples)
#             print("Context...addQPTRefs(): Adding {} triples brings Total to: {}".format(len(triples), len(self.qptRefs)))

    
        def getQPTRef(self, about):
            refs = []
            for n in self.qptRefs:
                if n.referred == about:
                    refs.append(n)
            if len(refs) == 1: return refs[0]
            assert len(refs) == 0, "Did not expect more than one matches for {}".format(about)
            return None
                   
        def getQPTRefs(self):
            refs = []
            for n in self.qptRefs:
                refs.append(n)
            return refs
                   
        def translateTo(self, tgtEntity):
            '''
            Translates all triples in the query that this Association relates to. Translation implies the exchange of the 
            current IRI's with the IRI of the target entity. This is an in-place replacement.
            Output: N/A
            '''
            assert isinstance(tgtEntity, mediatorTools._Entity), "Context.QPTripleAssociation.translate(): Cannot translate without a target entity, got '{}'".format(repr(tgtEntity))
            for qptRef in self.getQPTRefs():
                assert qptRef.replaceWith(tgtEntity.getIriRef()), "Context.QueryPatternTripleAssociation.translateTo(): Cannot translate an already translated sparql node ({})".format(str(qptRef))
            
        def __str__(self):
            result = str(self.represents) + '\n\t'
            for n in self.qptRefs:
                result += str(n)
            return(result)

        def __repr__(self):
            result = str(self.represents) + '\n\t'
            for n in self.qptRefs:
                result += n.__repr__()
            return(result)
            

    class VarConstraints():
        '''
        The use of variables in the LIMIT clause of the sparql query is to bind constraints to the individuals of an entity. Constraints are specified
        by value logic expressions, and the entity individuals are specified by triple matches from the WHERE clause. 
        This class VarConstraints is designed to relate, through the use of the variables, the entities with the constraints. Each class object represents 
        the characteristics on how one variable is being bound to the constraints. These characteristics are determined by inspection of the parsed tree.
        '''
                
        def isBoundBy(self, qp_node):
            '''
            Utility function to establish whether a sparql tree node that occurs as BGP in the Query Pattern subtree, binds a variable that is part 
            of a sparql tree node that occurs in the Filter subtree as value logic tuple. 
            '''
            return(self._boundVar in qp_node.binds)
                
        
        class ValueLogicExpression(dict):
            '''
            A value logic expression represents a single comparison as can be found in the FILTER clause, e.g., (?t > 36.0), or (?p = ?q). Consequently, 
            it has three attributes, the varRef, the comparator, and the restriction. From a Correspondence perspective, this clause is used to express
            the entity restriction, as follows: 
            1. varRef (string): the name of the variable that is being used. Note that the entity that the variable refers to, represents a Path
            2. comparator (string)  : the comparator that is being used to establish the truth value of the boolean expression. 
            3. restriction (string) : the value that acts as restriction for the varRef. This can either be a Value restriction, Type restriction or 
                Multiplicity restriction [formulae 2.23]. 
            '''
            
            def __init__(self, variable_node=None):
                '''
                Creating a ValueLogicExpression is achieved by parsing the query, starting at the [Var] node.  
                input: The node in the sparql tree that represents the variable
                The created object is a Dictionary with entries {'varRef': varRef, 'comparator': comparator, 'restriction': restriction}
                '''
                
                assert isinstance(variable_node, ParseStruct) and type(variable_node).__name__ == 'Var', \
                    "Cannot create a Value Logic Constraint without a 'Var' to start with, got '{}'".format(type(variable_node))
#                 print("Elaborating on valueLogic {} ({}) in tree: ".format(variable_node, type(variable_node)))
#                 print(variable_node.dump())
                # Get the list of ancestors of this node in the tree
                ancestors = variable_node.getAncestors()
                prevAncestor = None
                restriction = None
                comparator = None
                varRef = None
                for ancestor in ancestors:
                    # For each ancestor, consider all its children.
                    ChildNodes = ancestor.getChildren()
                    if len(ChildNodes) > 1:
                        # This is a relevant branch since this parent has more children.
                        pType = type(ancestor).__name__
#                         print("parent <{}> is of type [{}]".format(ancestor, pType))
#                         print(ancestor.dump())
                        if pType == 'BuiltInCall':
                            raise NotImplementedError("Found '{}', hence a Type Constraint, which is not implemented (yet: be my guest)".format(variable_node))
                            break
                        elif pType == 'RelationalExpression':
#                             print('Build [{}]'.format(pType))
                            #TODO: Add the possibility for chained variables, i.e., [?t > ?v]
                            for childNode in ChildNodes:
#                                 print(">>\t'{}' is a [{}]:".format(childNode, type(childNode).__name__))
#                                 print(childNode.dump())
                                cType = type(childNode).__name__
                                if childNode == prevAncestor:
                                    # Since we go up and down the branch, we found the original value_node itself, that, therefore, is the varRef
                                    atom = childNode.descend()
                                    if atom == None: 
                                        AttributeError("QM node unexpectedly appears parent of more than one atomic path. Found <{}> with siblings. Hell will break loose, hence quitting".format(atom))
                                    varRef = atom
                                elif cType == 'NumericExpression':
                                    # This child is the top of branch leading to the restriction; 
                                    # The restriction is either a value, e.g., DECIMAL, or a variable
                                    atom = childNode.descend()
                                    if atom == None: 
                                        AttributeError("QM node unexpectedly appears parent of more than one atomic path. Found <{}> with siblings. Hell will break loose, hence quitting".format(atom))
                                    aType = type(atom).__name__
                                    if aType in ['INTEGER', 'DECIMAL', 'DOUBLE', 'HEX']:
#                                         print("NumericExpression as {} found".format(aType)) 
                                        restriction = atom
                                    elif aType == 'VAR1' or aType == 'VAR1':
                                        raise NotImplementedError('Chained variables in Query Modifiers not supported (yet, please implement me).')
                                        # One aspect of implementing this feature is:
#                                         restriction = atom 
                                        #TODO: (Chained variables in constraint) However, the translation and transformation is much harder
                                    else: AttributeError("Did not expect [{}] node in Query Modifier ({}), VAR's or Values only.".format(aType, atom))
                                elif str(childNode) in ['<', '<=', '>', '>=', '=', '!=']:
#                                     print("Comparator {} found".format(childNode)) 
                                    comparator = childNode
                                else:
                                    # Unknown 
                                    raise AttributeError("Unknown attribute: '{}'".format(childNode))
#                             print("ValueLogicExpression complete: ({} {} {})".format(varRef, comparator, restriction))

                            super().__init__({'varRef': varRef, 'comparator': comparator, 'restriction': restriction})
#                             self.update({'varRef': varRef, 'comparator': comparator, 'restriction': restriction})
                            break
                        else:
                            warnings.warn("Found '{}' element unexpectedly, ignoring".format(pType))
                    # This parent has only one child, which makes it an irrelevant node: only atomic or branching nodes are relevant; those
                    #        in between are not. But this node may show to be an interesting one, hence store it temporarily and continue with next parent.
                    prevAncestor = ancestor


        def __init__(self, sparql_tree=None, sparql_var_name='', entity=None):
            '''
            Input:
            - sparql_tree: (.ParseStruct) : The parsed tree must contain at least one 'FILTER' element, which must not be the tree root.
            - sparql_var_name: (string)    : The name of the sparql variable, including the question mark.
            - entity: ()    : the entity that this var is bound to
            Result: A class that contains the following attributes:
            - _boundVar: (string) : the name of the variable
            - _srcPath: the iriref of the path expression that leads to the Edoal <Property> or <Relation> element that this sparql variable is bound to
            - _valueLogicExprList: (list) : a list of (ValueLogicExpression) that represent the constraints that apply to this variable. Each element in the
                list represents a single constraint, e.g., "?var > 23.0", or "DATATYPE(?var) = '<iripath><class_name>'"
            '''
            
            #TODO: Consider the necessity of the two object variables _boundVar & entity_iri
            self._boundVar = ''       # (String) the name of the [Var] that has been bounded in the QueryPatternTripleAssociation; Necessary?? 
            self._entity = ''        # (mediatorTools._Entity) the entity that this var has been bound to
            self._valueLogicExprList = []    # list of (ValueLogicExpression)s, each of them formulating one single constraint, e.g., (?var > DECIMAL)
            
            assert isinstance(entity, mediatorTools._Entity), "Entity expected, got {}".format(type(entity))
            assert isinstance(sparql_tree, ParseStruct), "Parsed sparql tree expected, got {}".format(type(sparql_tree))
            assert sparql_tree != None or sparql_var_name != '', "Both parsed sparql tree and sparql variable required, but none found."
            filterElements = sparql_tree.searchElements(label="constraint")
            assert len(filterElements) == 1, "Do not support more than one FILTER clauses ({} found) in SPARQL (yet, please implement me)".format(len(filterElements))
            if filterElements == []:
                warnings.warn('Cannot find [FILTER] node in the sparql tree; constraint other than type <Filter> not yet implemented')
            else:
                self._entity = entity
                nodeType = SPARQLParser.Var   
                for fe in filterElements:
#                     print("Searching for <{}> as type <{}> in {}: ".format(sparql_var_name, nodeType, fe))
                    # Find the [ValueLogical](s) in this FILTER clause that address the variable, by searching for nodes: 
                    # 1 - of type 'SPARQLParser.Var', and 
                    # 2 - having a value that equals the variable name
                    # The variable can occur in more than one value logic expression, hence expect at least one node
                    varElements = fe.searchElements(element_type=nodeType, value=sparql_var_name)
                    if varElements == []:
                        warnings.warn('Cannot find <{}> as part of a [{}] expression in <{}>'.format(sparql_var_name, nodeType, fe))
                    else:
                        # We have found at least one such value logic expression, hence start to build it
                        self._boundVar = sparql_var_name
                        for ve in varElements:
#                             print ("var element:", ve)
                            vlExpr = self.ValueLogicExpression(variable_node=ve)
#                             print ("value logic expression: {}".format(vlExpr))
                            if vlExpr: self._valueLogicExprList.append(vlExpr)
        #                     print("Elaborating on valueLogic: ", v)
                    if len(self._valueLogicExprList) == 0:
                        warnings.warn('No applicable constraints (Query Modifiers) found for <{}>'.format(sparql_var_name))

        def getValueLogicExpressions(self):
            return self._valueLogicExprList
        
        def getBoundVar(self):
            return self._boundVar
        
        def getEntity(self):
            return self._entity
        
        def __str__(self):
            return self.__repr__()
        
        def __repr__(self):
            result = ''
            for vl in self.getValueLogicExpressions():
                #TODO: __str__() turn it into comma separated list of elements, as opposed to ( element ) ( element ) ...
                result += '( '+ str(vl) + ' ) '
            return(result)

        def render(self):
            print('constraints: \n' + self.__str__())


    def __init__(self, entity_type=SPARQLParser.IRIREF, *, entity_expression, sparqlTree, nsMgr ):
        '''
        Generate the sparql context that is associated with the EntityExpression. If no entity_type is given, assume an IRI type.
        Returns the context, contained in attributes:
        * entity_expr  : (mediator.mediatorTools.EntityExpression) the subject EntityExpression that this context is about (from input)
        * parsedQuery  : (ParseStruct) the parsed sparql-query-tree that requires translation (from input)
        * qptAssocs    : the query pattern triples in the sparql query that are associated by the subject Entities in the EntityExpression
        * constraints  : a list of the bound variables that occur in the qptAssocs, and for which a constraint in the sparql Query Pattern FILTER exists
        
        '''
        self.entity_expr = ''    # (mediator.mediatorTools.EntityExpression) The EDOAL entity_expression, i.e., one of (Class, Property, Relation, Instance) or their combinations, this context is about;
        self.parsedQuery = ''    # (parser.grammar.ParseInfo) The parsed query that is to be mediated
        self.qptAssocs = {}      # Dictionary of (QueryPatternTripleAssociation)s, indexed by the entity, representing the triples that are addressing the subject entity_expression
        self.constraints = {}    # Dictionary of (VarConstraints), indexed by the bound variables that occur in the qptAssocs, as contextualised Filters.
        self.nsMgr = None        # (namespaces.NSManager): the current nsMgr that can resolve any namespace issues of this mediator 
        
#         assert isinstance(entity_expression, Mediator.EntityExpression) and isinstance(sparqlTree, ParseStruct)
        assert isinstance(sparqlTree, ParseStruct) and isinstance(nsMgr, NSManager), "Context.__init__(): Parsed sparql request and namespace mgr required"
        assert isinstance(entity_expression, mediatorTools.EntityExpression), "Context.__init__(): Cannot create context without subject entity expression"

        # Initialize self
        self.nsMgr = nsMgr
#         print("Context.__init__(): entity expr: {}".format(entity_expression))
        self.entity_expr = entity_expression
        #TODO: process other sparqlData than sparql query, i.e., rdf triples or graph, and sparql result sets
        self.parsedQuery = sparqlTree
        if self.parsedQuery == []:
            raise RuntimeError("Context.__init__(): Cannot parse the query sparqlData")
        
        # 0: Consider the Entities that are part of the EntityExpression
        entities = []
        if self.entity_expr.isAtomicEntity():
            entities.append(self.entity_expr)
        else:
        #TODO: expand to the use of Entity Expressions
            raise NotImplementedError("Context.__init__(): Cannot process entity expressions yet, only simple entities. Got {}".format(entity_expression.getType()))

        for entity in entities:
            entityIRI = entity.getIriRef()
#             ePf, eIri, eTag = self.nsMgr.splitIri(entityIRI)
#             src_qname = ePf + ':' + eTag
    #         print("Context.__init__(): eeIri: {}".format(eeIri))
    #         print("Context.__init__(): eePf : {}".format(eePf))
    #         print("Context.__init__(): eeTag: {}".format(eeTag))
    #         print("Context.__init__(): eeOrg: {}".format(self.entity_expr.getIriRef()))
    #         print("Context.__init__(): entity type {}".format(entity_type))
            
            # 1: Find the qptRefs for which the context is to be build, matching the EntityIRI Name and its Type
            #TODO: When searching for QueryPatternTriple(s) in the SPARQL query that equal the entityIRI that is part of an EntityExpression, take CONTEXT, i.e. different parts of query, into account
            # Currently, ignore more than one query contexts and search the whole graph.
              
            srcNodes = self.parsedQuery.searchElements(element_type=entity_type, value=entityIRI)
#             print("Context.__init__(): Found graph nodes: ", srcNodes)
            if srcNodes == []: 
                warnings.warn("Context.__init__(): Cannot find element '{}' of type '{}' in sparqlData. Ignoring this entity".format(entityIRI, entity_type))
            else:
                # 2: Build the context: Query Pattern Triples part
                #    This stores all (references to) triples that refer to the entityIRI
                # More than one node can be the result for the search, for two reasons:
                #    1 - the searched value (now: entityIRI) matches triples from multiple contexts. The ignored issue here is that each context, e.g., [WhereClause], 
                #        can use a similar value for referring to a distinct thing (but not with IRI's)
                #    2 - the searched value (now: entityIRI) matches more triples in one context. The issue here is that while shared subjects, i.e., using ';', or shared
                #        properties, i.e., using ',', are shortcuts for full triple notation, the search result returns as many nodes as the IRI is used. 
                #        Hence, ( ?a IRI ?b. ?a IRI ?c. ) returns two nodes, while the equal ( ?a IRI ?b, ?c. ) returns one.
                #        Both shared and unshared IRI's must return an identical context. This is handled by collecting all triples first in one Object, and then storing 
                #        that object in self.
    
                # 2.1: First build a Query Pattern Triple Association for this entityIRI
                qptAssoc = self.QueryPatternTripleAssociation(entity=entity, sparql_tree=self.parsedQuery, nsMgr=self.nsMgr)
    
                # 2.2: For each node, add its qptRef(s) (multiplicity depends on whether the node is or is not shared amongst triples).
                for qrySrcNode in srcNodes:
                    # Collect all QueryPatternTriples of the main Node. Shared versus non-shared nodes are handled transparently by the addQPTRefs() method. 
                    qptAssoc.addQPTRefs(qrySrcNode)
                # Store the triples as associated to the subject entityIRI
                self.qptAssocs[entityIRI] = qptAssoc
                # First process all entities in the EntityExpression, since then all variables that are bound by this EntityExpression are known.
            
        # 3: Build the context: Query Modifiers part 
        #    (this addresses the constraints on the variables that are bound by the considered Query Patterns)
        # 3.1: Select the Query Modifiers top node in this Select Clause
        #TODO: Assuming one FILTER, i.e., one [GraphPatternNotTriples]   
        qmFilters = self.parsedQuery.searchElements(element_type=SPARQLParser.GraphPatternNotTriples)
        if qmFilters == []:
            warnings.warn("Context.__init__(): Cannot find Query Modifiers clause (Filter). Continuing without constraints.")
        else:
            # 3.2 - Collect the ValueLogics that represent the constraint of this entityExpression
            # 3.2.1 - Loop over all entities to collect all variables
            queryVars = set()
            for entity in entities:
                if entityIRI in self.qptAssocs:
                    qptAssoc = self.qptAssocs[entityIRI]
                    for qptRef in qptAssoc.qptRefs:
                        queryVars.update(qptRef.binds)
                else:
                    warnings.warn("Context.__init__(): No Query Pattern Triples have been associated with entity {}, hence bound variables are absent. Continuing without constraints.")
            # 3.2.2 - Loop over all queryVars and collect the ValueLogicExpression that addresses this variable
            for var in queryVars:
                self.constraints[str(var)] = []
                # Cycle over every FILTER subtree
                for qmFilter in qmFilters:
                    vc = self.VarConstraints(sparql_tree=qmFilter, sparql_var_name=str(var), entity = self.entity_expr)
                    self.constraints[str(var)].append(vc)


    def __str__(self):
        result = "<entity>: " + str(self.entity_expr) + "\n\thas nodes:"
        for qpt in self.qptAssocs:
            result += "\n\t-> " + str(qpt) 
        result += "\n\thas var constraints:"
        for vc in self.constraints:
            result += "\n\t-> " + str(vc) + ": "
            for vl in self.constraints[vc]:
                result += "\n\t\t " + str(vl)
        return(result)
        
    def render(self):
        print(self.__str__())
            


class SparqlQueryResultSet(): 
    '''
    This class represents a sparql query result set (SQRS), i.e., as specified by https://www.w3.org/TR/sparql11-overview/#sparql11-results
    Although all four different serialisation formats should be supported (XML, JSON, CSV and TSV), currently, only a JSON parser is supported.
    '''
    # Define the format identifiers
    Format = namedtuple('Format', ['json', 'xml', 'nvPairs'])
    qrsFormat = Format('JSON','XML', 'DICT')
    
    def __init__(self, result_set=None):
        '''
        Create a SQRS object from the input string.
        * input: a string that represents a SQRS. Currently only a JSON-formatted string is supported.
        Attributes:
            * qrsRaw - (string): The raw input query result set.
            * qrsFormat - (qrsFormat.xml, qrsFormat.json, qrsFormat.nvPairs): the format of the raw query result set; xml, json or dict, resp.
            * qType - (queryForm): the type of the query that led to this query result set
        '''
        # An SQRS can be transformed
        self.isTransformed = False
        # Determine the input type: a dict, an xml-string, or a json-string, and set the qrsFormat accordingly
        if isinstance(result_set, dict):
            assert "head" in result_set, "sparqlTools.sparqlQueryResultSet(): Cannot parse unknown dictionary structure, got {}".format(result_set)
            # Assume dictionary is structured according to query-result-set specification
            self.qrsRaw = json.loads(json.dumps(result_set))
            self.qrsFormat = SparqlQueryResultSet.qrsFormat.json
        elif isinstance(result_set, str):
            # Establish format by trying XML and JSON parser
            try:
                root = ElementTree.fromstring(result_set)
                # result_set could be parsed, hence the result_set was formatted as XML, hence store that fact
                self.qrsFormat = SparqlQueryResultSet.qrsFormat.xml
                #TODO: Parse XML-format
                raise NotImplementedError("sparqlTools.sparqlQueryResultSet(): Cannot parse an XML-formatted result string (yet, please implement  me)")
            except ParseError:
                try:
                    self.qrsRaw = json.loads(result_set)
                    # result_set could be parsed by json parser, hence the result_set was formatted as JSON, hence store that fact
                    self.qrsFormat = SparqlQueryResultSet.qrsFormat.json
                except JSONDecodeError as err:
                    raise NotImplementedError("sparqlTools.sparqlQueryResultSet(): Cannot parse query result set formatted as CSV or TSV (yet, please implement me")
        else:
            raise NotImplementedError("sparqlTools.sparqlQueryResultSet(): Cannot parse query result set formatted other than string or dict, got {}".format(type(result_set)))
        # Parse the (json) dict
        assert "head" in self.qrsRaw, "sparqlTools.sparqlQueryResultSet(): label 'head' expected in sparql result set, none found"
        if len(self.qrsRaw["head"]) > 0:
            # Result set is response to SELECT query
            self.qType = queryForm.select
#             self.bindings = self.qResult["results"]["bindings"]
        else:
            # Result set is response to ASK query
            self.qType = queryForm.ask

    def getVars(self):
        '''
        Get the variables that are used in this query result set
        '''
        assert self.qType == queryForm.select, "SparqlQueryResultSet.getVars(): Fatal - query result set of '{}' query does not contain variables.".format(self.qType)
        assert self.qrsFormat == SparqlQueryResultSet.qrsFormat.json, "SparqlQueryResultSet.getVars(): Fatal - only json-formats support (yet, please implement me)"
        return self.qrsRaw["head"]["vars"]
    
    def getBindings(self):
        '''
        Get the bindings results from this query result set
        '''
        assert self.qType == queryForm.select, "SparqlQueryResultSet.getBindings(): Fatal - query result set of '{}' query does not contain bindings.".format(self.qType)
        assert self.qrsFormat == SparqlQueryResultSet.qrsFormat.json, "SparqlQueryResultSet.getBindings(): Fatal - only json-formats support (yet, please implement me)"
        return self.qrsRaw["results"]["bindings"]
    
    def getQueryType(self):
        '''
        Return the type of query that this SQRS is a response to.
        returns one of: 'SELECT', 'ASK', 'CONSTRUCT', 'DESCRIBE'
        '''
        return self.qType
    
    def isResponseToASK(self):
        return self.getQueryType() == queryForm.ask
    def isResponseToSELECT(self):
        return self.getQueryType() == queryForm.select
    
    def hasSolution(self):
        '''
        Return whether or not a solution to the ASKed query pattern exists. 
        returns: True on available solution(s), False otherwise.
        '''
        assert self.qType == queryForm.ask, "SparqlQueryResultSet.getVars(): Fatal - cannot test the existence of a solution from a '{}' query.".format(self.qType)
        assert self.qrsFormat == SparqlQueryResultSet.qrsFormat.json, "SparqlQueryResultSet.getVars(): Fatal - got '{}', but only json-formats support (yet, please implement me)".format(self.qrsFormat)
        return self.qrsRaw["boolean"]

    def transform(self, var, corr):
        '''
        Transform the SQRS. 
        An SQRS consists of individuals only. Therefore, translations of entities are not applicable and hence no such method 
        exists on an SQRS. Contradictory, a transformation of individuals (values) is very relevant.
        The transformation is in-place, and one can only transform an SQRS once.  
        '''
        return None
        
    def __len__(self):
        '''
        Returns the length of the query solution, which equals the number of bindings to the variables tuple (as it was defined in the sparql query by 
        the SELECT-clause) that matched its Query Pattern and Modifier. 
        returns: 1 as response to ASK queries; the number of bindings as response to SELECT queries; None otherwise
        '''
        return len(self.qrsRaw["results"]["bindings"]) if self.isResponseToSELECT() else 1 if self.isResponseToASK() else None
        
    def __repr__(self):
        if self.isResponseToASK():
            return str(self.qType) + ': ' + str(self.qrsRaw["boolean"])
        elif self.isResponseToSELECT():
            return str(self.qType) + ': ' + str(self.getVars()) + ': ' + str(self.qrsRaw["results"]["bindings"])
        else:
            # Unknown type, hence return what is known...
            return str(self.qType, self.qrsRaw)
    
    def __str__(self):
        return (repr(self))
        
